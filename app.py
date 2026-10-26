"""YouTube Music source as an HTTP service for servers that speak NxtSound plugin protocol v1 (see the README)."""
import asyncio
import hmac
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import audio
import kept
import meta
import settings
import ytsource

log = logging.getLogger("plugin")

# plugin.json is the single source of truth for identity (id, name, version, description): the server reads it, and so does this file.
# Renaming the plugin therefore only means editing plugin.json.
SPEC = json.loads((Path(__file__).parent / "plugin.json").read_text(encoding="utf-8"))
VERSION = SPEC.get("version", "0.0.0")
TOKEN = os.getenv("PLUGIN_TOKEN", "")
ALLOW_NO_TOKEN = os.getenv("PLUGIN_ALLOW_NO_TOKEN", "") == "1"
API_TIMEOUT = float(os.getenv("API_TIMEOUT", "9"))  # just under the server's time limit (10 s)
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
BROWSE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{5,40}$")
API_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="api")

FEATURES = ["search", "stream", "radio", "artist", "album", "suggest", "prefetch"]
SEARCH_KINDS = ["songs", "artists", "albums"]


def build_manifest() -> dict:
    return {"id": SPEC["id"], "name": SPEC.get("name", SPEC["id"]), "version": VERSION, "api_version": 1,
            "description": SPEC.get("description", ""), "features": FEATURES, "search_kinds": SEARCH_KINDS, "settings": SPEC.get("settings", [])}


MANIFEST = build_manifest()


@asynccontextmanager
async def lifespan(_app):
    if not TOKEN and not ALLOW_NO_TOKEN:
        raise RuntimeError("PLUGIN_TOKEN is missing. Without a token anyone on the network can fetch audio (for testing only: PLUGIN_ALLOW_NO_TOKEN=1).")
    task = None
    if os.getenv("YTDLP_AUTOUPDATE", "1") != "0":  # tests and development: no self-update at all (otherwise the settings decide)
        audio.ready = asyncio.Event()
        task = asyncio.create_task(audio.update_loop())
    try:
        yield
    finally:
        if task:
            task.cancel()


app = FastAPI(title="YouTube Music source", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def auth(request: Request) -> None:
    if not TOKEN:
        return
    given = request.headers.get("authorization", "")
    if not hmac.compare_digest(given.encode(), f"Bearer {TOKEN}".encode()):
        raise HTTPException(401, "Token missing or wrong")


router = APIRouter(dependencies=[Depends(auth)])


async def _call(fn, *args):
    try:
        return await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(API_POOL, fn, *args), API_TIMEOUT)
    except asyncio.TimeoutError:
        raise HTTPException(504, "YouTube Music does not respond")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("%s fehlgeschlagen: %s", getattr(fn, "__name__", fn), e)
        raise HTTPException(502, "YouTube Music error")


def _video(video_id: str) -> str:
    if not VIDEO_ID_RE.match(video_id):
        raise HTTPException(404, "unknown ID")
    return video_id


def _browse(ref: str) -> str:
    if not BROWSE_ID_RE.match(ref):
        raise HTTPException(404, "unknown ID")
    return ref


@app.get("/health")
def health():
    return {"ok": True}


@router.get("/manifest")
def manifest():
    return build_manifest()


@router.get("/search")
async def search(q: str = Query(min_length=1, max_length=200), kind: str = "songs", limit: int = Query(25, ge=1, le=50)):
    if kind not in SEARCH_KINDS:
        raise HTTPException(400, "unknown kind")
    return {"items": await _call(ytsource.search, q, kind, limit)}


@router.get("/suggest")
async def suggest(q: str = Query(min_length=1, max_length=100)):
    return {"suggestions": await _call(ytsource.suggest, q)}


@router.get("/radio/{video_id}")
async def radio(video_id: str, limit: int = Query(40, ge=1, le=100)):
    """YouTube's songs for this one, with songs kept in the library in between (setting "Kept songs in the radio")."""
    found = await _call(ytsource.radio, _video(video_id), limit)
    seed = meta.lookup(video_id) or {}
    return {"tracks": kept.blend(found, video_id, seed.get("artist", ""), limit, settings.get("radio_mix"), audio.IMPORT_DIR)}


@router.get("/artist/{artist_id}")
async def artist(artist_id: str):
    page = await _call(ytsource.artist, _browse(artist_id))
    if not page:
        raise HTTPException(404, "Artist not found")
    return page


@router.get("/album/{browse_id}")
async def album(browse_id: str):
    page = await _call(ytsource.album, _browse(browse_id))
    if not page:
        raise HTTPException(404, "Album not found")
    return page


@router.get("/stream/{video_id}")
async def stream(video_id: str):
    vid = _video(video_id)
    local = audio.kept_file(vid)  # a copy in the library first: no download, nothing asked from YouTube
    if local:
        return FileResponse(local, media_type=audio.media_type(local))
    try:
        path = await audio.ensure_cached(vid)
    except audio.Timeout as e:
        raise HTTPException(504, str(e))
    except audio.Unavailable as e:
        raise HTTPException(502, f"Download failed: {e}")
    try:
        audio.adopt(vid)
    except OSError as e:
        log.info("Could not look for a kept copy of %s: %s", vid, e)
    return FileResponse(path, media_type=audio.media_type(path))  # Starlette serves Range requests


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def _info() -> list[dict]:
    """Read-only facts the server shows below the settings."""
    s = audio.state
    never = {"en": "never", "de": "nie"}
    files = [p for p in audio.CACHE_DIR.glob("*.*") if p.suffix not in (".part", ".ytdl", ".tmp")] if audio.CACHE_DIR.exists() else []
    out = [
        {"label": {"en": "yt-dlp version", "de": "yt-dlp-Version"}, "value": s.get("version") or "?"},
        {"label": {"en": "Last update check", "de": "Letzte Update-Suche"}, "value": _when(s.get("checked", 0)) or never},
        {"label": {"en": "Last update installed", "de": "Letztes Update installiert"}, "value": _when(s.get("updated", 0)) or never},
    ]
    if s.get("error"):
        out.append({"label": {"en": "Update error", "de": "Update-Fehler"}, "value": s["error"]})
    out.append({"label": {"en": "Cache", "de": "Cache"}, "value": f"{len(files)} / {sum(p.stat().st_size for p in files) // 1024 // 1024} MB"})
    out.append({"label": {"en": "Songs kept in the library", "de": "Titel in der Bibliothek behalten"}, "value": str(kept.count())})
    return out


@router.get("/settings")
def get_settings():
    return {"info": _info()}


@router.put("/settings")
async def put_settings(request: Request):
    """The admin changed the settings: the complete set arrives as {"values": {...}} and takes effect at once."""
    try:
        values = (await request.json())["values"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Expected {\"values\": {...}}")
    settings.update(values)
    return {"info": _info()}


class _Hint(BaseModel):
    ids: list[str] = Field(max_length=20)


@router.post("/prefetch", status_code=202)
async def prefetch(hint: _Hint):
    """The server tells which tracks are queued next; as many as the setting 'Songs to load ahead' allows are fetched in the background."""
    return {"accepted": audio.prefetch([i for i in hint.ids if VIDEO_ID_RE.match(i)])}


@router.get("/status")
def status():
    """State of yt-dlp (version, last check) and the current settings; for troubleshooting."""
    return {"version": VERSION, "yt_dlp": audio.state, "settings": settings.values}


app.include_router(router)
