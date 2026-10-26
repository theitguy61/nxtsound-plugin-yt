"""The songs this plugin kept in the library (settings: "Also keep in the library").

A kept song is a file in the server's library. This index says which YouTube ID it belongs to, so that
- playing the song again uses that file and asks YouTube for nothing,
- the radio can mix kept songs in between the ones YouTube suggests (so a library that grows is heard, and the radio is not YouTube's alone).

The file `kept.json` in the plugin's data folder holds ID -> {path (below the import folder), title, artist, album, duration, thumb, …}. A path whose
file is gone (the song was deleted in the library) is forgotten when it is looked up; playing it then simply downloads it again.
"""
import json
import logging
import math
import os
import random
import threading
from pathlib import Path

log = logging.getLogger("kept")

FILE = Path(os.getenv("PLUGIN_DATA_DIR", Path(__file__).parent / "data")) / "kept.json"
FIELDS = ("title", "artist", "artist_id", "album", "album_id", "duration", "thumb")
SHARE = {"off": 0.0, "some": 0.25, "many": 0.5}  # of the songs in a radio, how many come from the kept ones

_items: dict[str, dict] = {}
_lock = threading.Lock()


def load() -> None:
    try:
        raw = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    with _lock:
        _items.clear()
        _items.update({k: v for k, v in raw.items() if isinstance(v, dict) and isinstance(v.get("path"), str)} if isinstance(raw, dict) else {})


def _save() -> None:
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_name(FILE.name + ".tmp")
        tmp.write_text(json.dumps(_items), encoding="utf-8")
        os.replace(tmp, FILE)
    except OSError as e:
        log.warning("The list of kept songs could not be saved: %s", e)


def add(video_id: str, rel_path: str, track: dict) -> None:
    entry = {"path": rel_path, **{k: str(track.get(k) or "") for k in FIELDS}}
    with _lock:
        if _items.get(video_id) == entry:
            return
        _items[video_id] = entry
        _save()


def forget(video_id: str) -> None:
    with _lock:
        if _items.pop(video_id, None) is not None:
            _save()


def count() -> int:
    with _lock:
        return len(_items)


def path_of(video_id: str, root: Path | None) -> Path | None:
    """The kept file of a song, or None (not kept, or the file is gone)."""
    if root is None:
        return None
    with _lock:
        entry = _items.get(video_id)
    if not entry:
        return None
    p = root / entry["path"]
    try:
        if p.resolve().is_relative_to(root.resolve()) and p.is_file():
            return p
    except OSError:
        pass
    forget(video_id)
    return None


def _first_artist(name: str) -> str:
    return (name or "").split(",")[0].strip().lower()


def blend(yt_tracks: list[dict], seed_id: str, seed_artist: str, limit: int, mode: str, root: Path | None, dice=random) -> list[dict]:
    """The radio's songs from YouTube with kept songs in between. `mode`: off, some (about one in four), many (about every second).

    Kept songs of the seed's artist come first (up to half of them), the others are drawn at random, so the mix is not the same every time.
    Nothing is mixed in that YouTube suggested already, and the list never gets longer than `limit`."""
    share = SHARE.get(mode, 0.0)
    if not share or root is None:
        return yt_tracks
    taken = {t["id"] for t in yt_tracks} | {seed_id}
    with _lock:
        pool = [(vid, e) for vid, e in _items.items() if vid not in taken]
    if not pool:
        return yt_tracks
    cap = limit if not yt_tracks else max(1, math.ceil(len(yt_tracks) * share / (1 - share)))  # a short answer from YouTube is not drowned by kept songs
    want = min(round(limit * share), cap, len(pool))
    artist = _first_artist(seed_artist)
    same = [x for x in pool if artist and _first_artist(x[1]["artist"]) == artist]
    other = [x for x in pool if x not in same]
    dice.shuffle(same)
    dice.shuffle(other)
    head = same[: (want + 1) // 2]
    ordered = head + [x for x in other] + same[len(head):]
    chosen = []
    for vid, e in ordered:
        if len(chosen) >= want:
            break
        if path_of(vid, root):  # a copy that was deleted meanwhile does not count
            chosen.append({"id": vid, **{k: e[k] for k in FIELDS}})
    if not chosen:
        return yt_tracks
    dice.shuffle(chosen)
    out = yt_tracks[: max(0, limit - len(chosen))]
    n, total = len(chosen), len(out) + len(chosen)
    for i, t in enumerate(chosen):  # spread them evenly over the list: inserting from the front puts each at its final place
        out.insert(min(round((i + 0.5) * total / n), len(out)), t)
    return out


load()
