import asyncio
import aiohttp
import zlib
import json
import time
import logging
from typing import Optional
from fastapi import HTTPException

logger = logging.getLogger(__name__)


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

WS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Origin": "https://discord.com",
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


class GatewaySession:
    """
    Long-lived gateway connection for a single user token.
    Handles heartbeat, reconnects, and exposes a send/recv interface.
    Shared across scrape requests so we never IDENTIFY twice quickly.
    """

    IDENTIFY_COOLDOWN = 6.0  # seconds between IDENTIFYs

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
        self._lock = asyncio.Lock()
        self._recv_queue: asyncio.Queue = asyncio.Queue()
        self._recv_task: Optional[asyncio.Task] = None
        self._last_identify = 0.0
        self._connected = False
        self._last_used = time.monotonic()
        self._guilds_channels: dict[str, str] = {}
        self._guilds_roles: dict[str, dict] = {}

    async def ensure_connected(self, emit):
        """Connect (or reconnect) if not already live. Safe to call concurrently."""
        self._last_used = time.monotonic()
        async with self._lock:
            if self._connected and self._ws and not self._ws.closed:
                return
            logger.info("Gateway session connecting...")
            await self._connect(emit)

    async def _connect(self, emit):
        """Full connect + IDENTIFY cycle with rate-limit protection."""
        await self._teardown()

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
        self._guilds_channels = {}

        while True:
            if time.monotonic() > deadline:
                raise HTTPException(504, "Timed out waiting for READY.")
            msg = await self._raw_recv(timeout=10)
            op, t, s = msg.get("op"), msg.get("t"), msg.get("s")
            if s: self._seq = s
            if op == 9:
                raise HTTPException(401, "Discord rejected the session (Invalid Session). Check your token.")
            if op == 7:
                await emit("Discord requested reconnect, retrying...")
                await self._connect(emit)
                return
            if t == "READY":
                self._session_id = msg["d"].get("session_id")
                self._resume_url = msg["d"].get("resume_gateway_url")
                for g in msg["d"].get("guilds", []):
                    gid = str(g.get("id", ""))
                    for ch in g.get("channels", []):
                        if ch.get("type") == 0:
                            self._guilds_channels[gid] = ch["id"]
                            break
                    for r in g.get("roles", []):
                        self._guilds_roles.setdefault(gid, {})[r["id"]] = {
                            "id": r["id"], "name": r["name"],
                            "color": r.get("color", 0), "position": r.get("position", 0),
                        }
                await emit("Authenticated ✓")
                break

        self._connected = True
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
            except Exception as e:
                logger.warning("Heartbeat failed: %s — closing WebSocket", e)
                if self._ws and not self._ws.closed:
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
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
        while not self._recv_queue.empty():
            try: self._recv_queue.get_nowait()
            except: break

    def get_channel_for_guild(self, guild_id: str) -> Optional[str]:
        return self._guilds_channels.get(str(guild_id))

    def get_roles_for_guild(self, guild_id: str) -> dict:
        return self._guilds_roles.get(str(guild_id), {})


# Global session cache: token -> GatewaySession
_sessions: dict[str, GatewaySession] = {}
_sessions_lock = asyncio.Lock()

SESSION_MAX_IDLE = 1800  # 30 minutes
SESSION_MAX_COUNT = 10


async def get_session(token: str) -> GatewaySession:
    async with _sessions_lock:
        # Evict idle sessions
        now = time.monotonic()
        stale = [t for t, s in _sessions.items() if now - s._last_used > SESSION_MAX_IDLE]
        for t in stale:
            logger.info("Evicting idle gateway session")
            try:
                await _sessions[t]._teardown()
            except Exception:
                pass
            del _sessions[t]

        # Cap total sessions
        if token not in _sessions and len(_sessions) >= SESSION_MAX_COUNT:
            oldest_token = min(_sessions, key=lambda t: _sessions[t]._last_used)
            logger.info("Session cache full, evicting oldest session")
            try:
                await _sessions[oldest_token]._teardown()
            except Exception:
                pass
            del _sessions[oldest_token]

        if token not in _sessions:
            _sessions[token] = GatewaySession(token)
        session = _sessions[token]
        session._last_used = now
        return session
