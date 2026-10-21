"""What the plugin knows about tracks it has shown (title, artist, album).

yt-dlp only reports clean artist/album data for some uploads; the search, radio and album pages (ytmusicapi) know them for nearly every
song. They are remembered here so a song kept in the library gets a proper name and folder.
"""
import threading
from collections import OrderedDict

MAX_ITEMS = 3000
_items: OrderedDict[str, dict] = OrderedDict()
_lock = threading.Lock()


def remember(video_id: str, title: str, artist: str, album: str) -> None:
    if not video_id or not title:
        return
    with _lock:
        _items[video_id] = {"title": title, "artist": artist, "album": album}
        _items.move_to_end(video_id)
        while len(_items) > MAX_ITEMS:
            _items.popitem(last=False)


def lookup(video_id: str) -> dict | None:
    with _lock:
        return _items.get(video_id)
