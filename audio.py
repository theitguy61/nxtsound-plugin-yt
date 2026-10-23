"""Fetching audio: yt-dlp downloads the track into a cache (LRU by size), the stream serves the file.

YouTube keeps breaking yt-dlp; therefore yt-dlp can update itself at startup and regularly afterwards (pip, without root).
What happens is up to the admin's settings (see settings.py): update on/off, how often, stable or nightly; how many upcoming songs
are fetched ahead; whether songs are also kept permanently in the server's library (the import folder).
"""
import asyncio
import importlib
import importlib.metadata
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import meta
import settings
import yt_dlp

log = logging.getLogger("audio")

CACHE_DIR = Path(os.getenv("CACHE_DIR", Path(__file__).parent / "cache"))
CACHE_LIMIT_BYTES = int(float(os.getenv("CACHE_LIMIT_GB", "20")) * 1024**3)
YTDLP_CACHE_DIR = Path(os.getenv("YTDLP_CACHE_DIR", CACHE_DIR.parent / "ytdlp-cache"))
DL_TIMEOUT = float(os.getenv("DL_TIMEOUT", "120"))
IMPORT_DIR = Path(os.getenv("PLUGIN_IMPORT_DIR", "")) if os.getenv("PLUGIN_IMPORT_DIR") else None  # the server's import folder (part of its library)
FAIL_TTL = 60  # seconds during which a failed track is not tried again

_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="dl")
_sem: asyncio.Semaphore | None = None
_locks: dict[str, asyncio.Lock] = {}
_fails: dict[str, tuple[float, str]] = {}
state = {"version": "", "checked": 0.0, "updated": 0.0, "error": ""}
ready: asyncio.Event | None = None  # set as soon as the first yt-dlp check after startup is done


class Unavailable(Exception):
    """The track cannot be fetched (blocked, removed, network)."""


class Timeout(Exception):
    """Download takes too long."""


MEDIA = {".m4a": "audio/mp4", ".mp4": "audio/mp4", ".webm": "audio/webm", ".opus": "audio/ogg", ".ogg": "audio/ogg", ".mp3": "audio/mpeg"}


def media_type(p: Path) -> str:
    return MEDIA.get(p.suffix.lower(), "audio/mpeg")


def cached_path(video_id: str) -> Path | None:
    for p in CACHE_DIR.glob(f"{video_id}.*"):
        if p.suffix not in (".part", ".ytdl", ".tmp"):
            return p
    return None


def _opts(extra: dict | None = None) -> dict:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    YTDLP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return {"format": "bestaudio[ext=m4a]/bestaudio", "outtmpl": str(CACHE_DIR / "%(id)s.%(ext)s"), "quiet": True, "no_warnings": True,
            "noplaylist": True, "retries": 3, "socket_timeout": 20, "cachedir": str(YTDLP_CACHE_DIR), **(extra or {})}


def _download_sync(video_id: str, cancel: threading.Event) -> Path:
    def hook(_d: dict) -> None:
        if cancel.is_set():
            raise yt_dlp.utils.DownloadCancelled("abgebrochen")

    with yt_dlp.YoutubeDL(_opts({"progress_hooks": [hook]})) as ydl:
        info = ydl.extract_info(f"https://music.youtube.com/watch?v={video_id}", download=True)
    p = cached_path(video_id)
    if not p:
        raise RuntimeError("Download produced no file")
    try:
        keep(info or {}, p)
    except Exception as e:  # noqa: BLE001 (keeping a copy must never make the playback fail)
        log.warning("Could not keep %s in the library: %s", video_id, e)
    return p


# ---------- Keeping songs in the library ----------

_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _clean(name: str) -> str:
    """A text as part of a file name."""
    return re.sub(r"\s+", " ", _BAD_NAME.sub(" ", name or "")).strip(" .")[:100]


def describe(info: dict) -> tuple[str, str, str]:
    """(artist, title, album) of a downloaded track; '' where unknown. What the search, radio and album pages reported wins over yt-dlp's guess."""
    known = meta.lookup(info.get("id") or "")
    if known:
        return _clean(known["artist"].split(",")[0]), _clean(known["title"]), _clean(known["album"])
    artists = info.get("artists")
    artist = (artists[0] if isinstance(artists, list) and artists else "") or (info.get("artist") or "").split(",")[0].strip()
    title = info.get("track") or info.get("title") or ""
    if not artist:
        uploader = re.sub(r"\s*-\s*Topic$", "", info.get("uploader") or info.get("channel") or "")
        artist = uploader
        if " - " in title:  # "Artist - Title" uploads
            head, _, rest = title.partition(" - ")
            artist, title = head.strip(), rest.strip()
    return _clean(str(artist)), _clean(str(title)), _clean(str(info.get("album") or ""))


def keep_path(info: dict, src: Path) -> Path | None:
    """Where a downloaded track goes in the import folder: Artist/Album/Title.ext, or 'Artist - Title.ext' without an album."""
    artist, title, album = describe(info)
    if not title or IMPORT_DIR is None:
        return None
    ext = src.suffix.lower()
    if artist and album:
        return IMPORT_DIR / artist / album / f"{title}{ext}"
    return IMPORT_DIR / (f"{artist} - {title}{ext}" if artist else f"{title}{ext}")


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10) as r:  # noqa: S310 (https only, see _save_cover)
        return r.read(5_000_001)


def _save_cover(info: dict, dest: Path) -> None:
    """Puts the song's picture next to it (Title.jpg beside Title.m4a), which the server uses as the cover. A song without one is fine."""
    url = info.get("thumbnail") or ""
    if not url.startswith("https://"):
        return
    ext = Path(url.split("?")[0]).suffix.lower()
    side = dest.with_suffix(ext if ext in (".jpg", ".jpeg", ".png", ".webp") else ".jpg")
    try:
        data = _fetch(url)
        if 0 < len(data) <= 5_000_000:
            side.write_bytes(data)
    except Exception as e:  # noqa: BLE001
        log.info("No cover for %s: %s", dest.name, e)


def keep(info: dict, src: Path) -> Path | None:
    """If the admin chose to keep songs: copies the downloaded file into the import folder (once per song). Returns the target or None."""
    if settings.get("storage") != "library":
        return None
    dest = keep_path(info, src)
    if dest is None or dest.exists():
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name("." + dest.name + ".part")  # hidden until complete: the server's scan ignores names starting with a dot
    shutil.copyfile(src, tmp)
    _save_cover(info, dest)  # before the song appears, so the next scan finds both
    os.replace(tmp, dest)
    log.info("Kept in the library: %s", dest.relative_to(IMPORT_DIR))
    return dest


def evict_if_needed() -> None:
    """Deletes the longest-unused files until the cache is below the limit."""
    if CACHE_LIMIT_BYTES <= 0:
        return
    files = [p for p in CACHE_DIR.glob("*.*") if p.suffix not in (".part", ".ytdl", ".tmp")]
    total = sum(p.stat().st_size for p in files)
    for p in sorted(files, key=lambda p: p.stat().st_mtime):
        if total <= CACHE_LIMIT_BYTES:
            break
        try:
            size = p.stat().st_size
            p.unlink()
            total -= size
        except OSError:
            pass


async def ensure_cached(video_id: str) -> Path:
    p = cached_path(video_id)
    if p:
        p.touch()  # LRU: zuletzt benutzt
        return p
    hit = _fails.get(video_id)
    if hit and time.time() - hit[0] < FAIL_TTL:
        raise Unavailable(hit[1])
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(3)
    lock = _locks.setdefault(video_id, asyncio.Lock())
    try:
        async with lock:
            p = cached_path(video_id)
            if p:
                return p
            async with _sem:
                await _wait_ready()
                cancel = threading.Event()
                fut = asyncio.get_running_loop().run_in_executor(_pool, _download_sync, video_id, cancel)
                try:
                    for attempt in (1, 2):  # YouTube occasionally returns a one-off 403; an immediate second attempt usually helps
                        try:
                            p = await asyncio.wait_for(asyncio.shield(fut), DL_TIMEOUT)
                            break
                        except asyncio.TimeoutError:
                            raise
                        except Exception:  # noqa: BLE001
                            if attempt == 2:
                                raise
                            fut = asyncio.get_running_loop().run_in_executor(_pool, _download_sync, video_id, cancel)
                except asyncio.TimeoutError as e:
                    cancel.set()
                    raise Timeout("Download takes too long") from e
                except Exception as e:  # noqa: BLE001
                    msg = (str(e).splitlines() or ["Download fehlgeschlagen"])[-1][:200]
                    _fails[video_id] = (time.time(), msg)
                    raise Unavailable(msg) from e
            await asyncio.get_running_loop().run_in_executor(None, evict_if_needed)
            return p
    finally:
        _locks.pop(video_id, None)


# ---------- Fetching ahead ----------

_background: set[asyncio.Task] = set()
MAX_BACKGROUND = 10


async def _fetch_quietly(video_id: str) -> None:
    try:
        await ensure_cached(video_id)
    except (Unavailable, Timeout) as e:
        log.info("Fetching ahead failed for %s: %s", video_id, e)
    except Exception as e:  # noqa: BLE001
        log.warning("Fetching ahead failed for %s: %s", video_id, e)


def prefetch(video_ids: list[str]) -> int:
    """Starts fetching the first songs of the queue (as many as the setting allows) in the background; returns how many were started."""
    started = 0
    for vid in video_ids[:settings.get("prefetch")]:
        if cached_path(vid) or len(_background) >= MAX_BACKGROUND:
            continue
        task = asyncio.get_running_loop().create_task(_fetch_quietly(vid))
        _background.add(task)
        task.add_done_callback(_background.discard)
        started += 1
    return started


# ---------- yt-dlp-Aktualisierung ----------

def _installed_version() -> str:
    importlib.invalidate_caches()
    try:
        return importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        return ""


def _pip_update(channel: str) -> tuple[bool, str]:
    cmd = [sys.executable, "-m", "pip", "install", "-U", "--no-cache-dir", "--disable-pip-version-check", "-q"]
    if channel == "nightly":
        cmd.append("--pre")
    elif "dev" in _installed_version():
        cmd.append("--force-reinstall")  # pip never goes from a nightly (newer, pre-release) back to stable on its own
    cmd.append("yt-dlp[default]")  # [default] brings yt-dlp-ejs (solutions for YouTube's JavaScript checks)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stderr.strip().splitlines() or ["pip fehlgeschlagen"])[-1][:200]
    return True, ""


def _reload() -> None:
    """Loads the freshly installed yt-dlp into the running process; running downloads finish with the old version."""
    global yt_dlp
    for name in [n for n in sys.modules if n == "yt_dlp" or n.startswith("yt_dlp.")]:
        del sys.modules[name]
    importlib.invalidate_caches()
    yt_dlp = importlib.import_module("yt_dlp")


def update_sync() -> None:
    before = _installed_version()
    channel = settings.get("channel")
    ok, err = _pip_update(channel)
    after = _installed_version()
    state.update(checked=time.time(), error="" if ok else err, channel=channel)
    if ok and after and after != before:
        _reload()
        state["updated"] = time.time()
        log.info("yt-dlp aktualisiert: %s -> %s", before or "?", after)
    state["version"] = after or before


_wake: asyncio.Event | None = None


def _settings_changed(changed: set) -> None:
    """Admin changed the update settings: the loop wakes up (and checks at once if updating is on)."""
    if changed & {"auto_update", "update_hours", "channel"} and _wake is not None and _loop is not None:
        try:
            _loop.call_soon_threadsafe(_wake.set)
        except RuntimeError:  # the loop has already ended (shutdown)
            pass


_loop: asyncio.AbstractEventLoop | None = None
settings.on_change(_settings_changed)


async def update_loop() -> None:
    """Checks for a new yt-dlp at startup and then every `update_hours` hours, as long as `auto_update` is on.

    Changing one of the update settings wakes the loop: with updating on it checks at once (this is how a switch of the branch takes effect)."""
    global _wake, _loop
    _wake, _loop = asyncio.Event(), asyncio.get_running_loop()
    try:
        await _update_cycle()
    finally:
        _wake = _loop = None


async def _update_cycle() -> None:
    while True:
        auto = settings.get("auto_update")
        if auto:
            try:
                await asyncio.get_running_loop().run_in_executor(None, update_sync)
            except Exception as e:  # noqa: BLE001 (a failed update must never disturb)
                state["error"] = str(e)[:200]
                log.warning("yt-dlp-Update: %s", e)
        else:
            state["version"] = _installed_version()
        if ready is not None:
            ready.set()
        try:
            await asyncio.wait_for(_wake.wait(), settings.get("update_hours") * 3600 if auto else None)
        except asyncio.TimeoutError:
            pass
        _wake.clear()


async def _wait_ready() -> None:
    """The first download after startup briefly waits for the check so it runs with the newest version."""
    if ready is not None and not ready.is_set():
        try:
            await asyncio.wait_for(ready.wait(), 90)
        except asyncio.TimeoutError:
            pass
