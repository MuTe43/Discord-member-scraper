import asyncio
import aiohttp
import time
import logging
from fastapi import HTTPException

from gateway import get_session

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v9"


def _parse_member(m: dict) -> dict | None:
    user = m.get("user", {})
    if user.get("bot"):
        return None
    uid = user.get("id")
    if not uid:
        return None
    return {
        "id": uid,
        "name": m.get("nick") or user.get("global_name") or user.get("username"),
        "username": user.get("username"),
        "avatar": user.get("avatar"),
        "joined_at": m.get("joined_at"),
        "roles": m.get("roles", []),
        "quirks": [], "notes": "",
    }
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


# ─── Scraper using persistent session ─────────────────────────────────────────
async def scrape_gateway(token: str, guild_id: str, progress_q: asyncio.Queue) -> list[dict]:
    async def emit(msg: str):
        await progress_q.put({"type": "progress", "text": msg})

    logger.info("Starting scrape for guild %s", guild_id)
    members: dict[str, dict] = {}
    gs = await get_session(token)

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
        from main import get_http_session
        http = await get_http_session()
        async with http.get(f"{DISCORD_API}/guilds/{guild_id}/channels", headers=headers) as resp:
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

        for op_item in d.get("ops", []):
            action = op_item.get("op")
            items = op_item.get("items", []) if action == "SYNC" else [op_item.get("item", {})]
            for item in items:
                m = item.get("member")
                if not m: continue
                parsed = _parse_member(m)
                if parsed and parsed["id"] not in members:
                    members[parsed["id"]] = parsed

        if d.get("member_count"):
            total_members = d["member_count"]
            await emit(f"Server has {total_members} members, planning ranges...")

    # ── Step 2: pre-calculate ALL ranges needed, send each separately ─────────
    all_ranges = []
    start = 0
    while start < total_members:
        all_ranges.append([start, min(start + 99, total_members - 1)])
        start += 100

    await emit(f"Fetching {len(all_ranges)} ranges for {total_members} members...")

    for i, rang in enumerate(all_ranges):
        range_start, range_end = rang

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
                    parsed = _parse_member(m)
                    if parsed and parsed["id"] not in members:
                        members[parsed["id"]] = parsed
                if action == "SYNC":
                    got_sync = True

        await emit(f"Range {i+1}/{len(all_ranges)} done — {len(members)}/{total_members} members")

        if i < len(all_ranges) - 1:
            await asyncio.sleep(0.35)

    after_ranges = len(members)
    await emit(f"Range sweep done — {after_ranges}/{total_members} members. Running search sweep for stragglers...")

    # ── Step 3: alphabet search sweep via op 8 ────────────────────────────────
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

        chunk_deadline = time.monotonic() + 8
        got_chunk = False
        while not got_chunk:
            if time.monotonic() > chunk_deadline:
                break
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
            chunk_nonce = d.get("nonce", nonce)
            if chunk_nonce != nonce and chunk_nonce != "":
                continue

            for m in d.get("members", []):
                parsed = _parse_member(m)
                if parsed and parsed["id"] not in members:
                    members[parsed["id"]] = parsed
                    swept += 1

            got_chunk = True

        if len(members) >= total_members:
            break

        await asyncio.sleep(0.3)

    if swept > 0:
        await emit(f"Search sweep found {swept} additional members.")

    await emit(f"Done — {len(members)}/{total_members} members collected ✓")
    logger.info("Scrape complete for guild %s: %d/%d members", guild_id, len(members), total_members)
    return list(members.values())


# ─── REST helpers ──────────────────────────────────────────────────────────────
async def fetch_guild_info(token: str, guild_id: str) -> dict:
    from main import get_http_session
    headers = {**HTTP_HEADERS, "Authorization": token}
    session = await get_http_session()
    async with session.get(f"{DISCORD_API}/guilds/{guild_id}", headers=headers) as resp:
        if resp.status == 401: raise HTTPException(401, "Invalid token.")
        if resp.status in (403, 404): return {"id": guild_id, "name": guild_id, "icon": None}
        if resp.status != 200: raise HTTPException(resp.status, await resp.text())
        return await resp.json()


async def fetch_user_guilds(token: str) -> list[dict]:
    from main import get_http_session
    headers = {**HTTP_HEADERS, "Authorization": token}
    session = await get_http_session()
    async with session.get(f"{DISCORD_API}/users/@me/guilds", headers=headers) as resp:
        if resp.status == 401: raise HTTPException(401, "Invalid token.")
        if resp.status != 200: raise HTTPException(resp.status, await resp.text())
        return await resp.json()
