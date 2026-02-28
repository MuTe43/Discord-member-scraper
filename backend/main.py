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

# Discord gateway close codes and whether they're recoverable
FATAL_CLOSE_CODES = {
    4004: "Token invalid.",
    4010: "Invalid shard.",
    4011: "Sharding required.",
    4012: "Invalid API version.",
    4013: "Invalid intents.",
    4014: "Disallowed intents.",
}
# 4002 (decode error), 4001 (unknown opcode), 4003 (not authenticated),
# 4005 (already authenticated), 4007 (invalid seq), 4009 (session timeout)
# are all recoverable with a fresh connect + identify.

WS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Origin": "https://discord.com",
}
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


# ─── Persistent gateway session ───────────────────────────────────────────────
# One session per token, kept alive between scrapes.
# Key: token string  Value: GatewaySession instance

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


class GatewaySession:
    """
    Long-lived gateway connection for a single user token.
    Handles heartbeat, reconnects, and exposes a send/recv interface.
    Shared across scrape requests so we never IDENTIFY twice quickly.
    """

    IDENTIFY_COOLDOWN = 6.0  # seconds between IDENTIFYs (Discord allows 1/5s, we add buffer)

    def __init__(self, token: str):
        self.token = token
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._zs = ZlibStream()
        self._hb_task: Optional[asyncio.Task] = None
        self._seq = None
        self._session_id = None
        self._resume_url = None
        self._ready = False
        self._lock = asyncio.Lock()          # prevents concurrent connect attempts
        self._recv_queue: asyncio.Queue = asyncio.Queue()
        self._recv_task: Optional[asyncio.Task] = None
        self._last_identify = 0.0
        self._connected = False

    async def ensure_connected(self, emit):
        """Connect (or reconnect) if not already live. Safe to call concurrently."""
        async with self._lock:
            if self._connected and self._ws and not self._ws.closed:
                return
            await self._connect(emit)

    async def _connect(self, emit):
        """Full connect + IDENTIFY cycle with rate-limit protection."""
        # Tear down any existing connection cleanly
        await self._teardown()

        # Rate limit: don't IDENTIFY more than once per IDENTIFY_COOLDOWN seconds
        since_last = time.monotonic() - self._last_identify
        if since_last < self.IDENTIFY_COOLDOWN:
            wait = self.IDENTIFY_COOLDOWN - since_last
            await emit(f"Rate limiting — waiting {wait:.1f}s before reconnect...")
            await asyncio.sleep(wait)

        await emit("Connecting to Discord Gateway...")
        self._session = aiohttp.ClientSession()
        self._zs = ZlibStream()

        connect_url = self._resume_url or GATEWAY_URL
        self._ws = await self._session.ws_connect(
            connect_url, headers=WS_HEADERS, heartbeat=None, max_msg_size=0
        )

        # HELLO
        hello = await self._raw_recv(timeout=10)
        if hello.get("op") != 10:
            raise HTTPException(502, f"Expected HELLO, got op={hello.get('op')}")
        hb_interval = hello["d"]["heartbeat_interval"] / 1000
        await emit(f"Gateway hello ✓  (heartbeat {hb_interval:.0f}s)")

        # Start heartbeat
        self._hb_task = asyncio.create_task(self._heartbeat_loop(hb_interval))

        # IDENTIFY
        await emit("Identifying...")
        self._last_identify = time.monotonic()
        await self._raw_send({
            "op": 2,
            "d": {
                "token": self.token,
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

        # Wait for READY
        await emit("Waiting for READY...")
        deadline = time.monotonic() + 30
        self._guilds_channels = {}  # guild_id -> first text channel_id

        while True:
            if time.monotonic() > deadline:
                raise HTTPException(504, "Timed out waiting for READY.")
            msg = await self._raw_recv(timeout=10)
            op, t, s = msg.get("op"), msg.get("t"), msg.get("s")
            if s: self._seq = s
            if op == 9:
                raise HTTPException(401, "Discord rejected the session (Invalid Session). Check your token.")
            if op == 7:  # RECONNECT
                await emit("Discord requested reconnect, retrying...")
                await self._connect(emit)
                return
            if t == "READY":
                self._session_id = msg["d"].get("session_id")
                self._resume_url = msg["d"].get("resume_gateway_url")
                # Extract first text channel per guild from READY payload
                for g in msg["d"].get("guilds", []):
                    gid = str(g.get("id", ""))
                    for ch in g.get("channels", []):
                        if ch.get("type") == 0:
                            self._guilds_channels[gid] = ch["id"]
                            break
                await emit("Authenticated ✓")
                break

        self._connected = True
        # Start background recv pump that fills the queue
        self._recv_task = asyncio.create_task(self._recv_pump())

    async def _recv_pump(self):
        """Continuously reads from WebSocket and pushes to queue."""
        try:
            while self._ws and not self._ws.closed:
                try:
                    frame = await asyncio.wait_for(self._ws.receive(), timeout=60)
                except asyncio.TimeoutError:
                    continue
                if frame.type == aiohttp.WSMsgType.BINARY:
                    text = self._zs.push(frame.data)
                    if text:
                        msg = json.loads(text)
                        s = msg.get("s")
                        if s: self._seq = s
                        await self._recv_queue.put(msg)
                elif frame.type == aiohttp.WSMsgType.TEXT:
                    msg = json.loads(frame.data)
                    s = msg.get("s")
                    if s: self._seq = s
                    await self._recv_queue.put(msg)
                elif frame.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    code = frame.data
                    self._connected = False
                    await self._recv_queue.put({"_error": True, "_code": code, "_msg": str(frame.data)})
                    break
        except Exception as e:
            self._connected = False
            await self._recv_queue.put({"_error": True, "_code": 0, "_msg": str(e)})

    async def recv(self, timeout=20) -> dict:
        """Get next message from the queue."""
        msg = await asyncio.wait_for(self._recv_queue.get(), timeout=timeout)
        if msg.get("_error"):
            code = msg.get("_code", 0)
            text = msg.get("_msg", "Unknown")
            if code in FATAL_CLOSE_CODES:
                raise HTTPException(401, FATAL_CLOSE_CODES[code])
            raise ConnectionError(f"WebSocket closed: {text} (code {code})")
        return msg

    async def send(self, payload: dict):
        if not self._ws or self._ws.closed:
            raise ConnectionError("WebSocket is not connected")
        await self._raw_send(payload)

    async def _raw_recv(self, timeout=20) -> dict:
        """Direct recv from WS, used only during connect before pump starts."""
        deadline = time.monotonic() + timeout
        while True:
            rem = deadline - time.monotonic()
            if rem <= 0: raise asyncio.TimeoutError()
            frame = await asyncio.wait_for(self._ws.receive(), timeout=rem)
            if frame.type == aiohttp.WSMsgType.BINARY:
                text = self._zs.push(frame.data)
                if text: return json.loads(text)
            elif frame.type == aiohttp.WSMsgType.TEXT:
                return json.loads(frame.data)
            elif frame.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                code = getattr(frame.data, 'code', 0) if hasattr(frame.data, 'code') else 0
                if code in FATAL_CLOSE_CODES:
                    raise HTTPException(401, FATAL_CLOSE_CODES[code])
                raise ConnectionError(f"WebSocket closed: {frame.data}")

    async def _raw_send(self, payload: dict):
        await self._ws.send_str(json.dumps(payload))

    async def _heartbeat_loop(self, interval: float):
        await asyncio.sleep(interval * 0.4)
        while self._ws and not self._ws.closed:
            try:
                await self._raw_send({"op": 1, "d": self._seq})
                await asyncio.sleep(interval)
            except Exception:
                break

    async def _teardown(self):
        self._connected = False
        if self._hb_task:
            self._hb_task.cancel()
            self._hb_task = None
        if self._recv_task:
            self._recv_task.cancel()
            self._recv_task = None
        if self._ws and not self._ws.closed:
            try: await self._ws.close()
            except: pass
        if self._session and not self._session.closed:
            try: await self._session.close()
            except: pass
        self._ws = None
        self._session = None
        # Drain queue
        while not self._recv_queue.empty():
            try: self._recv_queue.get_nowait()
            except: break

    def get_channel_for_guild(self, guild_id: str) -> Optional[str]:
        return self._guilds_channels.get(str(guild_id))


# Global session cache: token -> GatewaySession
_sessions: dict[str, GatewaySession] = {}

def get_session(token: str) -> GatewaySession:
    if token not in _sessions:
        _sessions[token] = GatewaySession(token)
    return _sessions[token]


# ─── Scraper using persistent session ─────────────────────────────────────────
async def scrape_gateway(token: str, guild_id: str, progress_q: asyncio.Queue) -> list[dict]:
    async def emit(msg: str):
        await progress_q.put({"type": "progress", "text": msg})

    members: dict[str, dict] = {}
    gs = get_session(token)

    # Connect (or reuse existing connection)
    retries = 0
    while True:
        try:
            await gs.ensure_connected(emit)
            break
        except HTTPException:
            raise
        except Exception as e:
            retries += 1
            if retries >= 3:
                raise HTTPException(502, f"Failed to connect after {retries} attempts: {e}")
            wait = 2 ** retries
            await emit(f"Connection error ({e}), retrying in {wait}s...")
            await asyncio.sleep(wait)
            await gs._teardown()

    # Get channel_id — try from READY cache first, then REST fallback
    channel_id = gs.get_channel_for_guild(guild_id)
    if not channel_id:
        await emit("Fetching channels via REST...")
        headers = {**HTTP_HEADERS, "Authorization": token}
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{DISCORD_API}/guilds/{guild_id}/channels", headers=headers) as resp:
                if resp.status == 200:
                    channels = await resp.json()
                    text_chs = [c for c in channels if c.get("type") == 0]
                    if text_chs:
                        channel_id = text_chs[0]["id"]

    if not channel_id:
        raise HTTPException(400, "Couldn't find a text channel to subscribe to in this server.")

    await emit(f"Subscribing to member list...")

    # Send first op 14 to kick off the sync and get total_members
    await gs.send({
        "op": 14,
        "d": {
            "guild_id": guild_id,
            "channels": {channel_id: [[0, 99]]},
            "typing": True,
            "activities": True,
            "threads": False,
        }
    })

    # ── Step 1: send first op 14 to get total_members count ──────────────────
    total_members = None
    last_new = time.monotonic()

    await emit("Waiting for member count...")

    # Drain until we get the first GUILD_MEMBER_LIST_UPDATE with member_count
    deadline = time.monotonic() + 15
    while total_members is None:
        if time.monotonic() > deadline:
            raise HTTPException(408, "Timed out waiting for member list. No access to channels?")
        try:
            msg = await gs.recv(timeout=8)
        except asyncio.TimeoutError:
            raise HTTPException(408, "No member list response. You may not have access to any channels.")

        if msg.get("_error"):
            raise ConnectionError(msg.get("_msg", "WS error"))
        if msg.get("op") == 7:
            await emit("Reconnect requested, retrying...")
            await gs._teardown()
            await gs.ensure_connected(emit)
            await gs.send({"op": 14, "d": {"guild_id": guild_id, "channels": {channel_id: [[0, 99]]}, "typing": True, "activities": True, "threads": False}})
            continue
        if msg.get("t") != "GUILD_MEMBER_LIST_UPDATE":
            continue
        d = msg["d"]
        if str(d.get("guild_id")) != str(guild_id):
            continue

        # Collect members from this first batch
        for op_item in d.get("ops", []):
            action = op_item.get("op")
            items = op_item.get("items", []) if action == "SYNC" else [op_item.get("item", {})]
            for item in items:
                m = item.get("member")
                if not m: continue
                user = m.get("user", {})
                if user.get("bot"): continue
                uid = user.get("id")
                if uid and uid not in members:
                    members[uid] = {
                        "id": uid,
                        "name": m.get("nick") or user.get("global_name") or user.get("username"),
                        "username": user.get("username"),
                        "avatar": user.get("avatar"),
                        "joined_at": m.get("joined_at"),
                        "roles": m.get("roles", []),
                        "quirks": [], "notes": "",
                    }

        if d.get("member_count"):
            total_members = d["member_count"]
            await emit(f"Server has {total_members} members, planning ranges...")

    # ── Step 2: pre-calculate ALL ranges needed, send each separately ─────────
    # Discord op 14 treats each call as "this is my current viewport".
    # Sending all ranges in one payload only syncs the last one.
    # We must send each range as its own op 14, one at a time, waiting for
    # the SYNC response before requesting the next.

    all_ranges = []
    start = 0
    while start < total_members:
        all_ranges.append([start, min(start + 99, total_members - 1)])
        start += 100

    await emit(f"Fetching {len(all_ranges)} ranges for {total_members} members...")

    for i, rang in enumerate(all_ranges):
        range_start, range_end = rang

        # Skip if we already got most of these members (from overlapping batches)
        already_have = sum(1 for m in members.values() if True)  # we'll check after recv
        await gs.send({
            "op": 14,
            "d": {
                "guild_id": guild_id,
                "channels": {channel_id: [rang]},
                "typing": True,
                "activities": True,
                "threads": False,
            }
        })

        # Wait for the SYNC for this range
        range_deadline = time.monotonic() + 10
        got_sync = False
        while not got_sync:
            if time.monotonic() > range_deadline:
                await emit(f"Range {i+1}/{len(all_ranges)} timed out, skipping...")
                break
            try:
                msg = await gs.recv(timeout=5)
            except asyncio.TimeoutError:
                await emit(f"Range {i+1}/{len(all_ranges)} timed out, skipping...")
                break
            except ConnectionError as e:
                await emit(f"Connection dropped, reconnecting...")
                await gs._teardown()
                await gs.ensure_connected(emit)
                # Retry this range
                await gs.send({"op": 14, "d": {"guild_id": guild_id, "channels": {channel_id: [rang]}, "typing": True, "activities": True, "threads": False}})
                range_deadline = time.monotonic() + 10
                continue

            if msg.get("op") == 7:
                await emit("Reconnect requested...")
                await gs._teardown()
                await gs.ensure_connected(emit)
                await gs.send({"op": 14, "d": {"guild_id": guild_id, "channels": {channel_id: [rang]}, "typing": True, "activities": True, "threads": False}})
                range_deadline = time.monotonic() + 10
                continue

            if msg.get("t") != "GUILD_MEMBER_LIST_UPDATE":
                continue
            d = msg["d"]
            if str(d.get("guild_id")) != str(guild_id):
                continue

            for op_item in d.get("ops", []):
                action = op_item.get("op")
                if action not in ("SYNC", "INSERT", "UPDATE"):
                    continue
                items = op_item.get("items", []) if action == "SYNC" else [op_item.get("item", {})]
                for item in items:
                    m = item.get("member")
                    if not m: continue
                    user = m.get("user", {})
                    if user.get("bot"): continue
                    uid = user.get("id")
                    if uid and uid not in members:
                        members[uid] = {
                            "id": uid,
                            "name": m.get("nick") or user.get("global_name") or user.get("username"),
                            "username": user.get("username"),
                            "avatar": user.get("avatar"),
                            "joined_at": m.get("joined_at"),
                            "roles": m.get("roles", []),
                            "quirks": [], "notes": "",
                        }
                if action == "SYNC":
                    got_sync = True

        await emit(f"Range {i+1}/{len(all_ranges)} done — {len(members)}/{total_members} members")

        # Small delay between ranges so we don't flood the gateway
        if i < len(all_ranges) - 1:
            await asyncio.sleep(0.35)

    after_ranges = len(members)
    await emit(f"Range sweep done — {after_ranges}/{total_members} members. Running search sweep for stragglers...")

    # ── Step 3: alphabet search sweep via op 8 ────────────────────────────────
    # op 14 only returns members visible in the role-sorted sidebar.
    # Offline members with no hoisted role are invisible to op 14.
    # op 8 with a query string searches ALL members regardless of online status.
    # We sweep a-z + 0-9 to catch everyone missed by the range sweep.
    # Each query returns up to 100 results — good enough for name prefix coverage.

    SWEEP_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"
    nonce_base = str(int(time.time() * 1000))
    swept = 0

    for idx, char in enumerate(SWEEP_CHARS):
        nonce = f"{nonce_base}_{char}"
        await gs.send({
            "op": 8,
            "d": {
                "guild_id": guild_id,
                "query": char,
                "limit": 100,
                "presences": False,
                "nonce": nonce,
            }
        })

        # Collect GUILD_MEMBERS_CHUNK for this query
        chunk_deadline = time.monotonic() + 8
        got_chunk = False
        while not got_chunk:
            if time.monotonic() > chunk_deadline:
                break  # op 8 might be blocked for this server, just move on
            try:
                msg = await gs.recv(timeout=5)
            except asyncio.TimeoutError:
                break
            except ConnectionError:
                break

            if msg.get("t") != "GUILD_MEMBERS_CHUNK":
                continue
            d = msg["d"]
            if str(d.get("guild_id")) != str(guild_id):
                continue
            # Accept chunks with our nonce OR no nonce (some servers omit it)
            chunk_nonce = d.get("nonce", nonce)
            if chunk_nonce != nonce and chunk_nonce != "":
                continue

            for m in d.get("members", []):
                user = m.get("user", {})
                if user.get("bot"): continue
                uid = user.get("id")
                if uid and uid not in members:
                    members[uid] = {
                        "id": uid,
                        "name": m.get("nick") or user.get("global_name") or user.get("username"),
                        "username": user.get("username"),
                        "avatar": user.get("avatar"),
                        "joined_at": m.get("joined_at"),
                        "roles": m.get("roles", []),
                        "quirks": [], "notes": "",
                    }
                    swept += 1

            got_chunk = True

        # Stop early if we've found everyone
        if len(members) >= total_members:
            break

        await asyncio.sleep(0.3)

    if swept > 0:
        await emit(f"Search sweep found {swept} additional members.")

    await emit(f"Done — {len(members)}/{total_members} members collected ✓")
    return list(members.values())


# ─── REST helpers ──────────────────────────────────────────────────────────────
async def fetch_guild_info(token: str, guild_id: str) -> dict:
    headers = {**HTTP_HEADERS, "Authorization": token}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{DISCORD_API}/guilds/{guild_id}", headers=headers) as resp:
            if resp.status == 401: raise HTTPException(401, "Invalid token.")
            if resp.status in (403, 404): return {"id": guild_id, "name": guild_id, "icon": None}
            if resp.status != 200: raise HTTPException(resp.status, await resp.text())
            return await resp.json()

async def fetch_user_guilds(token: str) -> list[dict]:
    headers = {**HTTP_HEADERS, "Authorization": token}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{DISCORD_API}/users/@me/guilds", headers=headers) as resp:
            if resp.status == 401: raise HTTPException(401, "Invalid token.")
            if resp.status != 200: raise HTTPException(resp.status, await resp.text())
            return await resp.json()


# ─── API ───────────────────────────────────────────────────────────────────────
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
            if resp.status == 401: raise HTTPException(401, "Invalid token.")
            if resp.status != 200: raise HTTPException(resp.status, await resp.text())
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
                yield 'data: {"type":"error","detail":"Overall timeout."}\n\n'
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
    if not server: raise HTTPException(404, "Server not found.")
    return server["members"]

@app.patch("/api/servers/{guild_id}/members/{member_id}")
def update_member(guild_id: str, member_id: str, req: UpdateMemberRequest):
    data = load_data()
    server = data["servers"].get(guild_id)
    if not server: raise HTTPException(404, "Server not found.")
    member = server["members"].get(member_id)
    if not member: raise HTTPException(404, "Member not found.")
    if req.quirks is not None: member["quirks"] = req.quirks
    if req.notes is not None: member["notes"] = req.notes
    save_data(data)
    return member

@app.delete("/api/servers/{guild_id}")
def delete_server(guild_id: str):
    data = load_data()
    if guild_id not in data["servers"]: raise HTTPException(404, "Server not found.")
    del data["servers"][guild_id]
    save_data(data)
    return {"deleted": guild_id}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)