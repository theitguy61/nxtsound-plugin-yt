"""Access to YouTube Music (ytmusicapi) and mapping to the shapes of the plugin protocol."""
import logging
import threading
import time

from ytmusicapi import YTMusic

import meta

log = logging.getLogger("ytsource")

_tl = threading.local()
_radio_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}
RADIO_TTL = 600


def _yt() -> YTMusic:
    """One client per thread (so the session is never shared in parallel)."""
    inst = getattr(_tl, "yt", None)
    if inst is None:
        inst = YTMusic()
        _tl.yt = inst
    return inst


# ---------- Umsetzung ----------

def _thumb(item: dict) -> str:
    thumbs = item.get("thumbnails") or item.get("thumbnail") or []
    url = thumbs[-1].get("url", "") if thumbs and isinstance(thumbs[-1], dict) else ""
    return url if url.startswith("https://") else ""


def _artists(item: dict) -> tuple[str, str]:
    """(names joined with commas, id of the first artist)"""
    artists = item.get("artists") or item.get("author") or []
    if isinstance(artists, dict):
        artists = [artists]
    if isinstance(artists, list):
        names = [a.get("name", "") for a in artists if isinstance(a, dict) and a.get("name")]
        first_id = next((a.get("id") for a in artists if isinstance(a, dict) and a.get("id")), "") or ""
        return ", ".join(names), first_id
    return str(artists), ""


def track(item: dict) -> dict | None:
    vid = item.get("videoId")
    if not vid:
        return None
    artist, artist_id = _artists(item)
    album = item.get("album")
    out = {
        "id": vid, "title": item.get("title", ""), "artist": artist, "artist_id": artist_id,
        "album": album.get("name", "") if isinstance(album, dict) else "",
        "album_id": (album.get("id") or "") if isinstance(album, dict) else "",
        "duration": item.get("duration") or item.get("length") or "", "thumb": _thumb(item),
    }
    meta.remember(vid, out["title"], artist, out["album"], out)
    return out


def tracks(items) -> list[dict]:
    return [t for t in (track(i) for i in (items or [])) if t]


def album_card(item: dict) -> dict | None:
    bid = item.get("browseId")
    if not bid:
        return None
    artist, _ = _artists(item)
    return {"type": "album", "id": bid, "title": item.get("title", ""), "artist": artist, "year": item.get("year", ""),
            "kind": item.get("type", ""), "thumb": _thumb(item)}


# ---------- Protokoll-Funktionen (blockierend, im Thread-Pool aufrufen) ----------

def search(q: str, kind: str, limit: int = 25) -> list[dict]:
    res = _yt().search(q, filter=kind, limit=limit)
    items: list[dict] = []
    for r in res:
        if len(items) >= limit:  # ytmusicapi likes to return more than asked for
            break
        if kind == "songs":
            t = track(r)
            if t:
                items.append(t)
        elif kind == "artists":
            if r.get("browseId"):
                items.append({"type": "artist", "id": r["browseId"], "name": r.get("artist") or r.get("title", ""), "thumb": _thumb(r)})
        else:
            card = album_card(r)
            if card:
                items.append(card)
    return items


_suggest_cache: dict[str, tuple[float, list[str]]] = {}
SUGGEST_TTL = 600


def suggest(q: str) -> list[str]:
    """Search suggestions from YouTube Music while typing (cached briefly; typing produces many requests)."""
    key = q.strip().lower()
    hit = _suggest_cache.get(key)
    if hit and time.time() - hit[0] < SUGGEST_TTL:
        return hit[1]
    out = [s for s in _yt().get_search_suggestions(q) if isinstance(s, str) and s.strip()][:8]
    if len(_suggest_cache) >= 512:
        _suggest_cache.pop(min(_suggest_cache, key=lambda k: _suggest_cache[k][0]))
    _suggest_cache[key] = (time.time(), out)
    return out


def radio(video_id: str, limit: int = 40) -> list[dict]:
    key = (video_id, limit)
    hit = _radio_cache.get(key)
    if hit and time.time() - hit[0] < RADIO_TTL:
        return hit[1]
    res = _yt().get_watch_playlist(videoId=video_id, radio=True, limit=limit)
    out = [t for t in tracks(res.get("tracks")) if t["id"] != video_id][:limit]  # ytmusicapi likes to return more than asked for
    if len(_radio_cache) >= 256:
        _radio_cache.pop(min(_radio_cache, key=lambda k: _radio_cache[k][0]))
    _radio_cache[key] = (time.time(), out)
    return out


def _artist_fallback(artist_id: str) -> dict | None:
    """Channels without an artist page (e.g. pure upload channels): show the channel's videos."""
    try:
        u = _yt().get_user(artist_id)
    except Exception as e:  # noqa: BLE001 (ytmusicapi raises different errors depending on the situation)
        log.info("get_user %s: %s", artist_id, e)
        return None
    name = u.get("name") or ""
    songs = [{"id": v["videoId"], "title": v.get("title", ""), "artist": name, "artist_id": artist_id, "album": "", "album_id": "",
              "duration": v.get("duration") or "", "thumb": _thumb(v)} for v in (u.get("videos") or {}).get("results") or [] if v.get("videoId")]
    if not songs:
        return None
    return {"id": artist_id, "name": name, "description": "", "subscribers": "", "thumb": songs[0]["thumb"], "songs": songs,
            "albums": [], "singles": []}


def artist(artist_id: str) -> dict | None:
    try:
        a = _yt().get_artist(artist_id)
    except Exception as e:  # noqa: BLE001
        log.info("get_artist %s: %s", artist_id, e)
        return _artist_fallback(artist_id)
    return {
        "id": artist_id, "name": a.get("name", ""), "description": a.get("description") or "", "subscribers": a.get("subscribers") or "",
        "thumb": _thumb(a), "songs": tracks((a.get("songs") or {}).get("results")),
        "albums": [c for c in (album_card(x) for x in (a.get("albums") or {}).get("results", [])) if c],
        "singles": [c for c in (album_card(x) for x in (a.get("singles") or {}).get("results", [])) if c],
    }


def album(browse_id: str) -> dict | None:
    try:
        a = _yt().get_album(browse_id)
    except Exception as e:  # noqa: BLE001
        log.info("get_album %s: %s", browse_id, e)
        return None
    cover = _thumb(a)
    album_artist, album_artist_id = _artists(a)
    out = []
    for raw in a.get("tracks") or []:
        t = track(raw)
        if not t:
            continue
        t.update(thumb=t["thumb"] or cover, artist=t["artist"] or album_artist, artist_id=t["artist_id"] or album_artist_id,
                 album=a.get("title", ""), album_id=browse_id)
        out.append(t)
        meta.remember(t["id"], t["title"], t["artist"], t["album"], t)
    return {"id": browse_id, "title": a.get("title", ""), "kind": a.get("type", ""), "year": a.get("year", ""), "duration": a.get("duration", ""),
            "thumb": cover, "artist": album_artist, "artist_id": album_artist_id, "tracks": out}
