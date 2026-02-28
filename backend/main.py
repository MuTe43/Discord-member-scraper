import os
import json
import asyncio
import aiohttp
import zlib
import time
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional, AsyncGenerator
import uvicorn

app = FastAPI(title="Server Lens API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="../frontend"), name="static")

@app.get("/")
def root():
    return FileResponse("../frontend/index.html")

DATA_FILE = "data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"servers": {}}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

DISCORD_API = "https://discord.com/api/v9"
GATEWAY_URL = "wss://gateway.discord.gg/?encoding=json&v=9&compress=zlib-stream"
ZLIB_SUFFIX = b"\x00\x00\xff\xff"

WS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Origin": "https://discord.com",
}
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

class ZlibStream:
    def __init__(self):
        self._buf = bytearray()
        self._z = zlib.decompressobj()

    def push(self, data: bytes) -> Optional[str]:
        self._buf.extend(data)
        if len(self._buf) >= 4 and self._buf[-4:] == ZLIB_SUFFIX:
            text = self._z.decompress(bytes(self._buf)).decode("utf-8")
            self._buf.clear()
            return text
        return None


async def scrape_gateway(token: str, guild_id: str, progress_q: asyncio.Queue) -> list[dict]:
    """
    Uses op 14 (LAZY_REQUEST / guild subscribe) which is what the Discord client
    sends when you open a server and scroll the member list.
    
    Strategy:
    1. Connect + IDENTIFY
    2. Send op 14 with the first visible text channel to get GUILD_MEMBER_LIST_UPDATE
       with an "SYNC" op — this gives us the first ~100 members and the total count
    3. Keep requesting ranges [0,99], [100,199], ... until we have everyone
    """
    async def emit(msg: str):
        await progress_q.put({"type": "progress", "text": msg})

    members: dict[str, dict] = {}

    await emit("Connecting to Discord Gateway...")

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(GATEWAY_URL, headers=WS_HEADERS, heartbeat=None, max_msg_size=0) as ws:
            zs = ZlibStream()

            async def recv(timeout=20) -> dict:
                deadline = time.monotonic() + timeout
                while True:
                    rem = deadline - time.monotonic()
                    if rem <= 0:
                        raise asyncio.TimeoutError()
                    frame = await asyncio.wait_for(ws.receive(), timeout=rem)
                    if frame.type == aiohttp.WSMsgType.BINARY:
                        text = zs.push(frame.data)
                        if text:
                            return json.loads(text)
                    elif frame.type == aiohttp.WSMsgType.TEXT:
                        return json.loads(frame.data)
                    elif frame.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        raise ConnectionError(f"WebSocket closed: {frame.data}")

            async def send(payload: dict):
                await ws.send_str(json.dumps(payload))

            # HELLO
            hello = await recv(timeout=10)
            hb_interval = hello["d"]["heartbeat_interval"] / 1000
            await emit(f"Gateway connected ✓")

            hb_seq = [None]
            async def hb_loop():
                await asyncio.sleep(hb_interval * 0.4)
                while not ws.closed:
                    try:
                        await send({"op": 1, "d": hb_seq[0]})
                        await asyncio.sleep(hb_interval)
                    except:
                        break
            hb_task = asyncio.create_task(hb_loop())

            # IDENTIFY
            await emit("Identifying...")
            await send({
                "op": 2,
                "d": {
                    "token": token,
                    "capabilities": 16381,
                    "properties": {
                        "os": "Windows", "browser": "Chrome", "device": "",
                        "system_locale": "en-US", "browser_version": "120.0.0.0",
                        "os_version": "10", "referrer": "", "referring_domain": "",
                        "release_channel": "stable", "client_build_number": 260006,
                        "client_event_source": None,
                    },
                    "presence": {"status": "invisible", "since": 0, "activities": [], "afk": False},
                    "compress": False,
                    "client_state": {
                        "guild_versions": {}, "highest_last_message_id": "0",
                        "read_state_version": 0, "user_guild_settings_version": -1,
                    },
                },
            })

            # Wait for READY, grab channel list from guilds
            await emit("Waiting for READY...")
            channel_id = None
            deadline = time.monotonic() + 30
            while True:
                if time.monotonic() > deadline:
                    hb_task.cancel()
                    raise HTTPException(504, "Timed out waiting for READY.")
                msg = await recv(timeout=10)
                s = msg.get("s")
                if s: hb_seq[0] = s
                if msg.get("op") == 9:
                    hb_task.cancel()
                    raise HTTPException(401, "Invalid session — bad token.")
                if msg.get("t") == "READY":
                    # Find a text channel in this guild to subscribe to
                    for g in msg["d"].get("guilds", []):
                        if str(g.get("id")) == str(guild_id):
                            for ch in g.get("channels", []):
                                # type 0 = text channel
                                if ch.get("type") == 0:
                                    channel_id = ch["id"]
                                    break
                            break
                    await emit("Authenticated ✓")
                    break

            if not channel_id:
                # Fallback: fetch channels via REST
                await emit("Fetching channels via REST...")
                headers = {**HTTP_HEADERS, "Authorization": token}
                async with session.get(f"{DISCORD_API}/guilds/{guild_id}/channels", headers=headers) as resp:
                    if resp.status == 200:
                        channels = await resp.json()
                        text_channels = [c for c in channels if c.get("type") == 0]
                        if text_channels:
                            channel_id = text_channels[0]["id"]

            if not channel_id:
                hb_task.cancel()
                raise HTTPException(400, "Couldn't find a text channel to subscribe to.")

            await emit(f"Using channel {channel_id} for member list sync...")

            # ── op 14: Subscribe to guild, request member list ranges ──────────
            # First request: ranges [[0, 99]] to get the SYNC event with total count
            await send({
                "op": 14,
                "d": {
                    "guild_id": guild_id,
                    "channels": {
                        channel_id: [[0, 99]]
                    },
                    "typing": True,
                    "activities": True,
                    "threads": False,
                }
            })

            # Wait for GUILD_MEMBER_LIST_UPDATE with ops containing SYNC or INSERT
            await emit("Waiting for member list sync...")
            total_members = None
            
            # Collect until we stop getting new members
            last_new_members = time.monotonic()
            ranges_requested = set()
            ranges_requested.add((0, 99))
            
            while True:
                try:
                    msg = await recv(timeout=10)
                except asyncio.TimeoutError:
                    # No new data — if we have members, we're done
                    if members:
                        await emit(f"Sync complete — {len(members)} members collected.")
                        break
                    else:
                        hb_task.cancel()
                        raise HTTPException(408, "No member data received. You may not have access to any channels in this server.")

                s = msg.get("s")
                if s: hb_seq[0] = s

                if msg.get("t") != "GUILD_MEMBER_LIST_UPDATE":
                    continue

                d = msg["d"]
                if str(d.get("guild_id")) != str(guild_id):
                    continue

                # Total online/member count
                if total_members is None and d.get("member_count"):
                    total_members = d["member_count"]
                    await emit(f"Server has {total_members} members, syncing...")

                # Process ops
                new_this_batch = 0
                for op in d.get("ops", []):
                    action = op.get("op")
                    
                    if action in ("SYNC", "INSERT", "UPDATE"):
                        items = op.get("items", [])
                        if action == "INSERT":
                            items = [op.get("item", {})]
                        
                        for item in items:
                            m = item.get("member")
                            if not m:
                                continue
                            user = m.get("user", {})
                            if user.get("bot"):
                                continue
                            uid = user.get("id")
                            if uid and uid not in members:
                                members[uid] = {
                                    "id": uid,
                                    "name": m.get("nick") or user.get("global_name") or user.get("username"),
                                    "username": user.get("username"),
                                    "avatar": user.get("avatar"),
                                    "joined_at": m.get("joined_at"),
                                    "roles": m.get("roles", []),
                                    "quirks": [],
                                    "notes": "",
                                }
                                new_this_batch += 1

                if new_this_batch > 0:
                    last_new_members = time.monotonic()
                    await emit(f"Synced {len(members)}{f'/{total_members}' if total_members else ''} members...")

                # If we know total, request next ranges
                if total_members and len(members) < total_members:
                    # Calculate next range to request
                    next_start = len(ranges_requested) * 100
                    if next_start < total_members:
                        next_end = next_start + 99
                        r = (next_start, next_end)
                        if r not in ranges_requested:
                            ranges_requested.add(r)
                            # Build all requested ranges list
                            ranges_list = [[s, e] for s, e in sorted(ranges_requested)]
                            await send({
                                "op": 14,
                                "d": {
                                    "guild_id": guild_id,
                                    "channels": {channel_id: ranges_list},
                                    "typing": True,
                                    "activities": True,
                                    "threads": False,
                                }
                            })

                # Done if we have everyone, or no new members for 5 seconds
                if total_members and len(members) >= total_members:
                    await emit(f"All {len(members)} members synced ✓")
                    break
                if time.monotonic() - last_new_members > 5:
                    await emit(f"No new members for 5s — collected {len(members)} total.")
                    break

            hb_task.cancel()

    return list(members.values())


async def fetch_guild_info(token: str, guild_id: str) -> dict:
    headers = {**HTTP_HEADERS, "Authorization": token}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{DISCORD_API}/guilds/{guild_id}", headers=headers) as resp:
            if resp.status == 401:
                raise HTTPException(401, "Invalid token.")
            if resp.status in (403, 404):
                return {"id": guild_id, "name": guild_id, "icon": None}
            if resp.status != 200:
                raise HTTPException(resp.status, await resp.text())
            return await resp.json()


async def fetch_user_guilds(token: str) -> list[dict]:
    headers = {**HTTP_HEADERS, "Authorization": token}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{DISCORD_API}/users/@me/guilds", headers=headers) as resp:
            if resp.status == 401:
                raise HTTPException(401, "Invalid token.")
            if resp.status != 200:
                raise HTTPException(resp.status, await resp.text())
            return await resp.json()


class ScrapeRequest(BaseModel):
    token: str
    guild_id: str

class UpdateMemberRequest(BaseModel):
    quirks: Optional[list[str]] = None
    notes: Optional[str] = None

class ValidateTokenRequest(BaseModel):
    token: str


@app.post("/api/validate-token")
async def validate_token(req: ValidateTokenRequest):
    headers = {**HTTP_HEADERS, "Authorization": req.token}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{DISCORD_API}/users/@me", headers=headers) as resp:
            if resp.status == 401:
                raise HTTPException(401, "Invalid token.")
            if resp.status != 200:
                raise HTTPException(resp.status, await resp.text())
            user = await resp.json()
    guilds = await fetch_user_guilds(req.token)
    return {
        "user": {"id": user["id"], "username": user.get("username"), "global_name": user.get("global_name"), "avatar": user.get("avatar")},
        "guilds": [{"id": g["id"], "name": g["name"], "icon": g.get("icon"), "owner": g.get("owner", False)} for g in guilds],
    }


@app.post("/api/scrape")
async def scrape_server(req: ScrapeRequest):
    progress_q: asyncio.Queue = asyncio.Queue()

    async def run_scrape():
        try:
            guild_info = await fetch_guild_info(req.token, req.guild_id)
            members = await scrape_gateway(req.token, req.guild_id, progress_q)
            data = load_data()
            existing = data["servers"].get(req.guild_id, {})
            existing_members = existing.get("members", {})
            members_dict = {}
            for m in members:
                mid = m["id"]
                if mid in existing_members:
                    m["quirks"] = existing_members[mid].get("quirks", [])
                    m["notes"] = existing_members[mid].get("notes", "")
                members_dict[mid] = m
            data["servers"][req.guild_id] = {
                "id": req.guild_id,
                "name": guild_info.get("name", req.guild_id),
                "icon": guild_info.get("icon"),
                "member_count": len(members_dict),
                "members": members_dict,
            }
            save_data(data)
            await progress_q.put({"type": "done", "guild_id": req.guild_id, "name": guild_info.get("name", req.guild_id), "scraped": len(members_dict)})
        except HTTPException as e:
            await progress_q.put({"type": "error", "detail": e.detail})
        except Exception as e:
            await progress_q.put({"type": "error", "detail": str(e)})

    async def event_stream() -> AsyncGenerator[str, None]:
        task = asyncio.create_task(run_scrape())
        while True:
            try:
                item = await asyncio.wait_for(progress_q.get(), timeout=120)
            except asyncio.TimeoutError:
                yield 'data: {"type":"error","detail":"Overall timeout — no response from gateway."}\n\n'
                break
            yield f"data: {json.dumps(item)}\n\n"
            if item.get("type") in ("done", "error"):
                break
        task.cancel()

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/servers")
def get_servers():
    data = load_data()
    return {gid: {**s, "member_count": len(s.get("members", {}))} for gid, s in data["servers"].items()}

@app.get("/api/servers/{guild_id}/members")
def get_members(guild_id: str):
    data = load_data()
    server = data["servers"].get(guild_id)
    if not server:
        raise HTTPException(404, "Server not found.")
    return server["members"]

@app.patch("/api/servers/{guild_id}/members/{member_id}")
def update_member(guild_id: str, member_id: str, req: UpdateMemberRequest):
    data = load_data()
    server = data["servers"].get(guild_id)
    if not server:
        raise HTTPException(404, "Server not found.")
    member = server["members"].get(member_id)
    if not member:
        raise HTTPException(404, "Member not found.")
    if req.quirks is not None:
        member["quirks"] = req.quirks
    if req.notes is not None:
        member["notes"] = req.notes
    save_data(data)
    return member

@app.delete("/api/servers/{guild_id}")
def delete_server(guild_id: str):
    data = load_data()
    if guild_id not in data["servers"]:
        raise HTTPException(404, "Server not found.")
    del data["servers"][guild_id]
    save_data(data)
    return {"deleted": guild_id}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)