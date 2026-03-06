import os
import json
import asyncio
import logging
import tempfile
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from typing import AsyncGenerator
import aiohttp
import uvicorn

from models import ScrapeRequest, UpdateMemberRequest, ValidateTokenRequest
from scraper import scrape_gateway, fetch_guild_info, fetch_user_guilds
from gateway import _sessions, _sessions_lock

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v9"
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# Resolve paths relative to this file, not CWD
_BACKEND_DIR = Path(__file__).resolve().parent
_FRONTEND_DIR = _BACKEND_DIR.parent / "frontend"

# Environment configuration
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*").split(",")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

app = FastAPI(title="Server Lens API")
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])


# Security headers middleware
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' https://cdn.discordapp.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'"
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)
app.mount("/static", StaticFiles(directory=str(_FRONTEND_DIR)), name="static")


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/")
def root():
    return FileResponse(str(_FRONTEND_DIR / "index.html"))


DATA_FILE = str(_BACKEND_DIR / "data.json")
_data_lock = asyncio.Lock()

# Shared aiohttp session for REST calls
_http_session: aiohttp.ClientSession | None = None


async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"servers": {}}


def save_data(data):
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(_BACKEND_DIR), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, DATA_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ─── API Routes ────────────────────────────────────────────────────────────────

@app.post("/api/validate-token")
async def validate_token(req: ValidateTokenRequest):
    headers = {**HTTP_HEADERS, "Authorization": req.token}
    session = await get_http_session()
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
            async with _data_lock:
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
                roles = {}
                for r in guild_info.get("roles", []):
                    roles[r["id"]] = {
                        "id": r["id"], "name": r["name"],
                        "color": r.get("color", 0), "position": r.get("position", 0),
                    }
                data["servers"][req.guild_id] = {
                    "id": req.guild_id,
                    "name": guild_info.get("name", req.guild_id),
                    "icon": guild_info.get("icon"),
                    "member_count": len(members_dict),
                    "roles": roles,
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
        try:
            while True:
                try:
                    item = await asyncio.wait_for(progress_q.get(), timeout=120)
                except asyncio.TimeoutError:
                    yield 'data: {"type":"error","detail":"Overall timeout."}\n\n'
                    break
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("type") in ("done", "error"):
                    break
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

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
async def update_member(guild_id: str, member_id: str, req: UpdateMemberRequest):
    async with _data_lock:
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
async def delete_server(guild_id: str):
    async with _data_lock:
        data = load_data()
        if guild_id not in data["servers"]: raise HTTPException(404, "Server not found.")
        del data["servers"][guild_id]
        save_data(data)
    return {"deleted": guild_id}


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down — closing all gateway sessions...")
    async with _sessions_lock:
        for tok, sess in _sessions.items():
            try:
                await sess._teardown()
            except Exception:
                pass
        _sessions.clear()
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
        _http_session = None
    logger.info("Shutdown complete.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)