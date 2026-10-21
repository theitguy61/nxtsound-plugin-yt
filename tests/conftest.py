import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="plugin-test-")
os.environ["CACHE_DIR"] = str(Path(_tmp) / "cache")
os.environ["PLUGIN_TOKEN"] = "geheim"
os.environ["PLUGIN_DATA_DIR"] = str(Path(_tmp) / "data")  # settings.json lives here
os.environ["PLUGIN_IMPORT_DIR"] = str(Path(_tmp) / "imported")
os.environ["YTDLP_AUTOUPDATE"] = "0"  # no pip calls in the tests
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import audio  # noqa: E402
import ytsource  # noqa: E402

AUDIO = bytes(range(256)) * 16  # 4096 Bytes
REAL_DOWNLOAD = audio._download_sync  # the tests replace audio._download_sync; the real one is tested with a stand-in for yt-dlp


class FakeYT:
    """Returns answers similar to ytmusicapi (no network)."""

    def search(self, q, filter="songs", limit=25):
        if filter == "songs":
            return [{"videoId": "dQw4w9WgXcQ", "title": "Never Gonna Give You Up", "artists": [{"name": "Rick Astley", "id": "UCuAXFkgsw1L7xaCfnd5JJOw"}],
                     "album": {"name": "Whenever You Need Somebody", "id": "MPREb_abcdefghijk"}, "duration": "3:33",
                     "thumbnails": [{"url": "https://i.ytimg.com/small.jpg"}, {"url": "https://i.ytimg.com/big.jpg"}]},
                    {"title": "Ohne videoId faellt weg"}]
        if filter == "artists":
            return [{"browseId": "UCuAXFkgsw1L7xaCfnd5JJOw", "artist": "Rick Astley", "thumbnails": [{"url": "https://i.ytimg.com/a.jpg"}]}]
        return [{"browseId": "MPREb_abcdefghijk", "title": "Whenever You Need Somebody", "artists": [{"name": "Rick Astley"}], "year": "1987", "type": "Album",
                 "thumbnails": [{"url": "http://unsicher.example/x.jpg"}]}]

    def get_search_suggestions(self, q):
        return [q + " live", q + " remix", "", 5]

    def get_watch_playlist(self, videoId, radio, limit):
        return {"tracks": [{"videoId": videoId, "title": "Selbst"}, {"videoId": "9bZkp7q19f0", "title": "Anderer", "artists": [{"name": "X"}]}]}

    def get_artist(self, artist_id):
        if artist_id == "UCnoartistpage00000000":
            raise RuntimeError("no artist page")
        return {"name": "Rick Astley", "description": "Singer", "subscribers": "4M", "thumbnails": [{"url": "https://i.ytimg.com/a.jpg"}],
                "songs": {"results": [{"videoId": "dQw4w9WgXcQ", "title": "Never Gonna Give You Up", "artists": [{"name": "Rick Astley"}]}]},
                "albums": {"results": [{"browseId": "MPREb_abcdefghijk", "title": "Whenever", "year": "1987"}]}, "singles": {"results": []}}

    def get_user(self, channel_id):
        return {"name": "Nur Uploads", "videos": {"results": [{"videoId": "AAAAAAAAAAA", "title": "Upload", "duration": "2:00"}]}}

    def get_album(self, browse_id):
        if browse_id == "MPREbnotfound0000":
            raise RuntimeError("not found")
        return {"title": "Whenever You Need Somebody", "type": "Album", "year": "1987", "duration": "40 Minuten",
                "artists": [{"name": "Rick Astley", "id": "UCuAXFkgsw1L7xaCfnd5JJOw"}], "thumbnails": [{"url": "https://i.ytimg.com/cover.jpg"}],
                "tracks": [{"videoId": "dQw4w9WgXcQ", "title": "Never Gonna Give You Up"}]}


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    ytsource._radio_cache.clear()
    ytsource._suggest_cache.clear()
    audio._fails.clear()
    fake = FakeYT()
    monkeypatch.setattr(ytsource, "_yt", lambda: fake)
    calls = []

    def fake_download(video_id, cancel):
        calls.append(video_id)
        if video_id == "FAILFAILFAI":
            raise RuntimeError("Video unavailable")
        audio.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = audio.CACHE_DIR / f"{video_id}.m4a"
        p.write_bytes(AUDIO)
        return p

    monkeypatch.setattr(audio, "_download_sync", fake_download)
    for p in audio.CACHE_DIR.glob("*") if audio.CACHE_DIR.exists() else []:
        p.unlink()
    return calls


@pytest.fixture()
def client():
    with TestClient(appmod.app, headers={"Authorization": "Bearer geheim"}) as c:
        yield c


@pytest.fixture()
def anon():
    with TestClient(appmod.app) as c:
        yield c
