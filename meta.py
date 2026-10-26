"""What the plugin knows about tracks it has shown (title, artist, album, and the whole track as the server got it).

yt-dlp only reports clean artist/album data for some uploads; the search, radio and album pages (ytmusicapi) know them for nearly every
song. They are remembered here so a song kept in the library gets a proper name and folder, and so that a kept song can be listed again
(in the radio) like the server first saw it.
"""
import threading
from collections import OrderedDict

MAX_ITEMS = 3000
_items: OrderedDict[str, dict] = OrderedDict()
_lock = threading.Lock()


def remember(video_id: str, title: str, artist: str, album: str, track: dict | None = None) -> None:
    if not video_id or not title:
        return
    with _lock:
        old = _items.get(video_id) or {}
        _items[video_id] = {"title": title, "artist": artist, "album": album, "track": dict(track) if track else old.get("track")}
        _items.move_to_end(video_id)
        while len(_items) > MAX_ITEMS:
            _items.popitem(last=False)


def lookup(video_id: str) -> dict | None:
    with _lock:
        return _items.get(video_id)


def track_of(video_id: str) -> dict | None:
    """The track as the plugin reported it to the server (id, title, artist, album, duration, thumb, …), if it is still remembered."""
    with _lock:
        t = (_items.get(video_id) or {}).get("track")
        return dict(t) if t else None
