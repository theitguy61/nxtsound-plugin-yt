"""The plugin against the plugin protocol and its own logic (without network, YouTube is replaced)."""
import asyncio

import audio
import ytsource
from tests.conftest import AUDIO
from tests.vendor import plugincheck, pluginproto


def test_speaks_the_plugin_protocol(client, monkeypatch):
    """The same check the server provides for every plugin (copy in tests/vendor)."""
    problems = plugincheck.check("http://plugin.test", "geheim", "never", transport=_transport(client))
    assert problems == []


def _transport(client):
    return client._transport  # the TestClient talks to the ASGI app directly


def test_manifest_is_valid(client):
    m = pluginproto.validate_manifest(client.get("/manifest").json())
    assert m["id"] == "yt" and set(m["features"]) == pluginproto.FEATURES  # the id comes from plugin.json


def test_requires_token(anon):
    assert anon.get("/manifest").status_code == 401
    assert anon.get("/search", params={"q": "x"}, headers={"Authorization": "Bearer falsch"}).status_code == 401
    assert anon.get("/health").json() == {"ok": True}  # only the health check is open


def test_search_maps_all_kinds(client):
    songs = client.get("/search", params={"q": "never"}).json()["items"]
    assert songs == [{"id": "dQw4w9WgXcQ", "title": "Never Gonna Give You Up", "artist": "Rick Astley", "artist_id": "UCuAXFkgsw1L7xaCfnd5JJOw",
                      "album": "Whenever You Need Somebody", "album_id": "MPREb_abcdefghijk", "duration": "3:33", "thumb": "https://i.ytimg.com/big.jpg"}]
    artists = client.get("/search", params={"q": "rick", "kind": "artists"}).json()["items"]
    assert artists[0] == {"type": "artist", "id": "UCuAXFkgsw1L7xaCfnd5JJOw", "name": "Rick Astley", "thumb": "https://i.ytimg.com/a.jpg"}
    albums = client.get("/search", params={"q": "rick", "kind": "albums"}).json()["items"]
    assert albums[0]["id"] == "MPREb_abcdefghijk" and albums[0]["thumb"] == ""  # https images only
    assert client.get("/search", params={"q": "x", "kind": "videos"}).status_code == 400


def test_radio_excludes_seed(client):
    tracks = client.get("/radio/dQw4w9WgXcQ").json()["tracks"]
    assert [t["id"] for t in tracks] == ["9bZkp7q19f0"]
    assert client.get("/radio/zu-kurz").status_code == 404


def test_radio_respects_limit(client, monkeypatch):
    many = {"tracks": [{"videoId": f"{i:011d}", "title": f"T{i}", "artists": [{"name": "X"}]} for i in range(30)]}
    monkeypatch.setattr(ytsource._yt(), "get_watch_playlist", lambda videoId, radio, limit: many)
    assert len(client.get("/radio/dQw4w9WgXcQ", params={"limit": 5}).json()["tracks"]) == 5


def test_artist_page_and_fallback(client):
    a = client.get("/artist/UCuAXFkgsw1L7xaCfnd5JJOw").json()
    assert a["name"] == "Rick Astley" and a["songs"][0]["id"] == "dQw4w9WgXcQ" and a["albums"][0]["id"] == "MPREb_abcdefghijk"
    fb = client.get("/artist/UCnoartistpage00000000").json()
    assert fb["name"] == "Nur Uploads" and fb["songs"][0]["id"] == "AAAAAAAAAAA" and fb["albums"] == []


def test_album_page_fills_missing_fields(client):
    al = client.get("/album/MPREb_abcdefghijk").json()
    t = al["tracks"][0]
    assert al["title"] == "Whenever You Need Somebody" and al["artist_id"] == "UCuAXFkgsw1L7xaCfnd5JJOw"
    assert t["artist"] == "Rick Astley" and t["album_id"] == "MPREb_abcdefghijk" and t["thumb"] == "https://i.ytimg.com/cover.jpg"
    assert client.get("/album/MPREbnotfound0000").status_code == 404


def test_stream_downloads_once_and_serves_ranges(client, fakes):
    r = client.get("/stream/dQw4w9WgXcQ")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mp4" and r.content == AUDIO
    part = client.get("/stream/dQw4w9WgXcQ", headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == AUDIO[100:200] and part.headers["content-range"] == "bytes 100-199/4096"
    assert fakes == ["dQw4w9WgXcQ"]  # the second fetch comes from the cache
    assert client.get("/stream/not-valid").status_code == 404


def test_stream_failure_is_reported_and_remembered(client, fakes):
    r = client.get("/stream/FAILFAILFAI")
    assert r.status_code == 502 and "Video unavailable" in r.json()["detail"]
    assert client.get("/stream/FAILFAILFAI").status_code == 502
    assert fakes == ["FAILFAILFAI", "FAILFAILFAI"]  # one retry, then not again for a short while


def test_stream_retries_once_after_transient_error(client, monkeypatch):
    calls = []
    real = audio._download_sync

    def flaky(video_id, cancel):
        calls.append(video_id)
        if len(calls) == 1:
            raise RuntimeError("HTTP Error 403: Forbidden")
        audio.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = audio.CACHE_DIR / f"{video_id}.m4a"
        p.write_bytes(AUDIO)
        return p

    monkeypatch.setattr(audio, "_download_sync", flaky)
    assert client.get("/stream/TRANSIENT12").status_code == 200 and len(calls) == 2


def test_search_respects_limit(client):
    many = [{"videoId": f"{i:011d}", "title": f"T{i}", "artists": [{"name": "X"}]} for i in range(30)]
    ytsource._yt().search = lambda q, filter="songs", limit=25: many
    assert len(client.get("/search", params={"q": "x", "limit": 7}).json()["items"]) == 7


def test_cache_eviction_keeps_newest(monkeypatch):
    audio.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    import os
    for i, name in enumerate(["AAAAAAAAAA1", "AAAAAAAAAA2", "AAAAAAAAAA3"]):
        p = audio.CACHE_DIR / f"{name}.m4a"
        p.write_bytes(b"x" * 1000)
        os.utime(p, (1000 + i, 1000 + i))
    monkeypatch.setattr(audio, "CACHE_LIMIT_BYTES", 2500)
    audio.evict_if_needed()
    left = sorted(p.name for p in audio.CACHE_DIR.glob("AAAAAAAAAA*"))
    assert left == ["AAAAAAAAAA2.m4a", "AAAAAAAAAA3.m4a"]


def test_refuses_to_start_without_token(monkeypatch):
    import app as appmod
    monkeypatch.setattr(appmod, "TOKEN", "")
    monkeypatch.setattr(appmod, "ALLOW_NO_TOKEN", False)
    from fastapi.testclient import TestClient
    import pytest
    with pytest.raises(RuntimeError, match="PLUGIN_TOKEN"):
        with TestClient(appmod.app):
            pass


def test_suggestions(client):
    assert client.get("/suggest", params={"q": "radio"}).json() == {"suggestions": ["radio live", "radio remix"]}  # nonsense (empty, number) is dropped
    assert "suggest" in client.get("/manifest").json()["features"]


def test_identity_comes_from_plugin_json(client, monkeypatch):
    """Renaming the plugin (id, name, version, description) only means editing plugin.json."""
    import app as appmod
    monkeypatch.setattr(appmod, "SPEC", {"id": "yt", "name": "YT Music", "version": "9.9.9", "description": "Custom text"})
    monkeypatch.setattr(appmod, "VERSION", "9.9.9")
    m = client.get("/manifest").json()
    assert (m["id"], m["name"], m["version"], m["description"]) == ("yt", "YT Music", "9.9.9", "Custom text")
    assert "suggest" in m["features"]


def test_manifest_matches_plugin_json_on_disk(client):
    import json
    from pathlib import Path
    spec = json.loads((Path(__file__).resolve().parent.parent / "plugin.json").read_text())
    m = client.get("/manifest").json()
    assert (m["id"], m["name"], m["version"]) == (spec["id"], spec["name"], spec["version"])
