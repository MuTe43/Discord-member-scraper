"""
Run this directly: python debug_gateway.py
It will print every single raw message from the gateway so we can see what's happening.
"""
import asyncio
import aiohttp
import zlib
import json
import sys

TOKEN    = input("Token: ").strip()
GUILD_ID = input("Guild ID: ").strip()

GATEWAY_URL = "wss://gateway.discord.gg/?encoding=json&v=9&compress=zlib-stream"
ZLIB_SUFFIX = b"\x00\x00\xff\xff"

WS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Origin": "https://discord.com",
}

def log(tag, msg):
    print(f"[{tag}] {msg}", flush=True)

async def main():
    buf = bytearray()
    z   = zlib.decompressobj()

    def push(data: bytes):
        buf.extend(data)
        if len(buf) >= 4 and buf[-4:] == ZLIB_SUFFIX:
            text = z.decompress(bytes(buf)).decode()
            buf.clear()
            return json.loads(text)
        return None

    async with aiohttp.ClientSession() as session:
        log("WS", f"Connecting to {GATEWAY_URL}")
        async with session.ws_connect(GATEWAY_URL, headers=WS_HEADERS, heartbeat=None, max_msg_size=0) as ws:
            log("WS", "Connected")

            async def recv(timeout=15):
                deadline = asyncio.get_event_loop().time() + timeout
                while True:
                    rem = deadline - asyncio.get_event_loop().time()
                    if rem <= 0:
                        raise TimeoutError("recv timeout")
                    frame = await asyncio.wait_for(ws.receive(), timeout=rem)
                    log("FRAME", f"type={frame.type.name} len={len(frame.data) if frame.data else 0}")
                    if frame.type == aiohttp.WSMsgType.BINARY:
                        msg = push(frame.data)
                        if msg:
                            log("MSG", f"op={msg.get('op')} t={msg.get('t')} keys={list(msg.get('d', {}).keys()) if isinstance(msg.get('d'), dict) else msg.get('d')}")
                            return msg
                    elif frame.type == aiohttp.WSMsgType.TEXT:
                        msg = json.loads(frame.data)
                        log("MSG", f"op={msg.get('op')} t={msg.get('t')}")
                        return msg
                    elif frame.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        log("WS", f"CLOSED/ERROR: {frame.data}")
                        raise ConnectionError(str(frame.data))

            async def send(payload):
                log("SEND", f"op={payload.get('op')} t={payload.get('t', '')}")
                await ws.send_str(json.dumps(payload))

            # Step 1: HELLO
            hello = await recv(timeout=10)
            hb_ms = hello["d"]["heartbeat_interval"]
            log("HELLO", f"heartbeat_interval={hb_ms}ms")

            hb_seq = [None]

            async def hb_loop():
                await asyncio.sleep(hb_ms / 1000 * 0.4)
                while not ws.closed:
                    await send({"op": 1, "d": hb_seq[0]})
                    await asyncio.sleep(hb_ms / 1000)

            hb = asyncio.create_task(hb_loop())

            # Step 2: IDENTIFY
            await send({
                "op": 2,
                "d": {
                    "token": TOKEN,
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

            # Step 3: Wait for READY (may get many events first)
            log("WAIT", "Waiting for READY (printing all events)...")
            ready = False
            for _ in range(50):  # read up to 50 events looking for READY
                msg = await recv(timeout=15)
                s = msg.get("s")
                if s: hb_seq[0] = s
                if msg.get("t") == "READY":
                    log("READY", "Got READY ✓")
                    ready = True
                    break
                if msg.get("op") == 9:
                    log("ERROR", "Invalid session — bad token")
                    sys.exit(1)

            if not ready:
                log("ERROR", "Never got READY after 50 messages")
                sys.exit(1)

            # Step 4: REQUEST_GUILD_MEMBERS
            nonce = "debug123"
            await send({
                "op": 8,
                "d": {
                    "guild_id": GUILD_ID,
                    "query": "",
                    "limit": 0,
                    "presences": False,
                    "nonce": nonce,
                },
            })

            # Step 5: Watch for chunks (or anything else)
            log("WAIT", "Waiting for GUILD_MEMBERS_CHUNK (watching all events for 30s)...")
            total = 0
            for _ in range(200):
                try:
                    msg = await recv(timeout=30)
                except TimeoutError:
                    log("TIMEOUT", "No message in 30s")
                    break
                s = msg.get("s")
                if s: hb_seq[0] = s
                t = msg.get("t")
                if t == "GUILD_MEMBERS_CHUNK":
                    d = msg["d"]
                    count = len(d.get("members", []))
                    total += count
                    log("CHUNK", f"chunk {d.get('chunk_index')}/{d.get('chunk_count')} nonce={d.get('nonce')} members={count} total={total}")
                    if d.get("chunk_index", 0) + 1 >= d.get("chunk_count", 1):
                        log("DONE", f"All chunks received. Total members: {total}")
                        break

            hb.cancel()

asyncio.run(main())
