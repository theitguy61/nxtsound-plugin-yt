"""Songs kept in the library: played from there, remembered across restarts, mixed into the radio."""
import json
import random

import pytest

import audio
import kept
import meta
import settings
from tests.conftest import AUDIO

INFO = {"id": "dQw4w9WgXcQ", "track": "Never Gonna Give You Up", "artists": ["Rick Astley"], "album": "Whenever You Need Somebody"}


@pytest.fixture(autouse=True)
def library(tmp_path, monkeypatch):
    """Its own settings, import folder and list of kept songs for every test; the library mode is on."""
    monkeypatch.setattr(settings, "FILE", tmp_path / "settings.json")
    monkeypatch.setattr(audio, "IMPORT_DIR", tmp_path / "imported")
    monkeypatch.setattr(kept, "FILE", tmp_path / "kept.json")
    kept._items.clear()
    settings.values.clear()
    settings.values.update(settings.DEFAULTS)
    settings.update({"storage": "library"})
    yield tmp_path
    kept._items.clear()
    settings.values.clear()
    settings.values.update(settings.DEFAULTS)


def _keep(tmp_path, vid, title, artist, album="Album"):
    """Keeps a song the way a download does: known from the search, copied into the library."""
    meta.remember(vid, title, artist, album, {"id": vid, "title": title, "artist": artist, "artist_id": "", "album": album, "album_id": "", "duration": "3:00", "thumb": ""})
    src = tmp_path / f"{vid}.m4a"
    src.write_bytes(AUDIO)
    return audio.keep({"id": vid, "title": title}, src)


# ---------- Playing ----------

def test_keeping_a_song_remembers_where_it_is(library):
    src = library / "x.m4a"
    src.write_bytes(AUDIO)
    dest = audio.keep(INFO, src)
    assert audio.kept_file("dQw4w9WgXcQ") == dest
    assert json.loads((library / "kept.json").read_text())["dQw4w9WgXcQ"]["path"] == "Rick Astley/Whenever You Need Somebody/Never Gonna Give You Up.m4a"
    kept._items.clear()
    kept.load()  # a restart
    assert audio.kept_file("dQw4w9WgXcQ") == dest


def test_a_kept_song_plays_from_the_library_without_a_download(client, fakes, library):
    _keep(library, "AAAAAAAAAAA", "Kept Song", "Band")
    r = client.get("/stream/AAAAAAAAAAA")
    assert r.status_code == 200 and r.content == AUDIO and fakes == []  # nothing was fetched from YouTube
    assert not list(audio.CACHE_DIR.glob("AAAAAAAAAAA.*"))  # and nothing is stored a second time in the cache


def test_range_requests_work_on_the_library_copy(client, library):
    _keep(library, "AAAAAAAAAAA", "Kept Song", "Band")
    r = client.get("/stream/AAAAAAAAAAA", headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == AUDIO[10:20]


def test_a_song_that_is_not_kept_is_downloaded_as_before(client, fakes):
    assert client.get("/stream/BBBBBBBBBBB").status_code == 200
    assert fakes == ["BBBBBBBBBBB"]


def test_when_the_copy_is_deleted_the_song_is_downloaded_again(client, fakes, library):
    dest = _keep(library, "AAAAAAAAAAA", "Kept Song", "Band")
    dest.unlink()
    assert client.get("/stream/AAAAAAAAAAA").status_code == 200 and fakes == ["AAAAAAAAAAA"]
    assert kept.count() == 0  # forgotten, not looked for again


def test_an_entry_cannot_point_out_of_the_library(client, fakes, library):
    (library / "secret.txt").write_bytes(b"not music")
    kept.add("AAAAAAAAAAA", "../secret.txt", {})
    r = client.get("/stream/AAAAAAAAAAA")
    assert r.content == AUDIO and fakes == ["AAAAAAAAAAA"]  # downloaded, the file outside was not served


def test_a_copy_from_before_the_list_existed_is_found_when_the_song_is_played(client, fakes, library):
    """The song is in the cache and its copy is in the library, but nothing remembers the copy yet (kept with an older version)."""
    meta.remember("dQw4w9WgXcQ", "Never Gonna Give You Up", "Rick Astley", "Whenever You Need Somebody")
    old = library / "imported" / "Rick Astley" / "Whenever You Need Somebody" / "Never Gonna Give You Up.m4a"
    old.parent.mkdir(parents=True)
    old.write_bytes(AUDIO)
    audio.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (audio.CACHE_DIR / "dQw4w9WgXcQ.m4a").write_bytes(AUDIO)
    assert client.get("/stream/dQw4w9WgXcQ").status_code == 200
    assert audio.kept_file("dQw4w9WgXcQ") == old and fakes == []
    fakes.clear()
    (audio.CACHE_DIR / "dQw4w9WgXcQ.m4a").unlink()  # the cache is emptied: the library copy is still there
    assert client.get("/stream/dQw4w9WgXcQ").status_code == 200 and fakes == []


def test_nothing_is_adopted_in_cache_mode(library):
    settings.update({"storage": "cache"})
    meta.remember("dQw4w9WgXcQ", "Never Gonna Give You Up", "Rick Astley", "Whenever You Need Somebody")
    audio.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (audio.CACHE_DIR / "dQw4w9WgXcQ.m4a").write_bytes(AUDIO)
    audio.adopt("dQw4w9WgXcQ")
    assert kept.count() == 0


def test_prefetch_skips_kept_songs(client, fakes, library):
    _keep(library, "AAAAAAAAAAA", "Kept Song", "Band")
    settings.update({"prefetch": 3})
    assert client.post("/prefetch", json={"ids": ["AAAAAAAAAAA", "BBBBBBBBBBB"]}).json() == {"accepted": 1}


# ---------- The radio ----------

def _songs(tmp_path, n, artist="Other", start=0):
    for i in range(start, start + n):
        _keep(tmp_path, f"K{i:010d}", f"Kept {i}", artist)


def test_the_radio_mixes_kept_songs_in(client, library):
    _songs(library, 12)
    tracks = client.get("/radio/dQw4w9WgXcQ", params={"limit": 8}).json()["tracks"]
    ids = [t["id"] for t in tracks]
    mine = [i for i in ids if i.startswith("K")]
    assert len(mine) == 1 and "dQw4w9WgXcQ" not in ids and len(set(ids)) == len(ids)  # YouTube's stand-in answers with one song: one kept song joins it
    assert "9bZkp7q19f0" in ids  # YouTube's own song is still there
    assert all(t["title"] and "thumb" in t for t in tracks)  # complete tracks, like any other


def test_many_mixes_every_second_song(client, library):
    _songs(library, 12)
    yt = [{"id": f"Y{i:010d}", "title": f"Y{i}", "artist": "x"} for i in range(8)]
    out = kept.blend(yt, "SEEDSEEDSEE", "", 8, "many", audio.IMPORT_DIR)
    assert sum(t["id"].startswith("K") for t in out) == 4 and len(out) == 8
    out = kept.blend(yt, "SEEDSEEDSEE", "", 8, "some", audio.IMPORT_DIR)
    assert sum(t["id"].startswith("K") for t in out) == 2 and len(out) == 8


def test_a_short_answer_from_youtube_is_not_drowned(library):
    _songs(library, 30)
    yt = [{"id": f"Y{i:010d}", "title": f"Y{i}", "artist": "x"} for i in range(2)]
    out = kept.blend(yt, "SEEDSEEDSEE", "", 40, "many", audio.IMPORT_DIR)
    assert len(out) == 4 and sum(t["id"].startswith("K") for t in out) == 2  # at most as many kept songs as YouTube's, not twenty


def test_when_youtube_has_nothing_the_kept_songs_make_the_radio(library):
    _songs(library, 30)
    out = kept.blend([], "SEEDSEEDSEE", "", 40, "some", audio.IMPORT_DIR)
    assert len(out) == 10 and all(t["id"].startswith("K") for t in out)


def test_off_leaves_the_radio_to_youtube(client, library):
    _songs(library, 12)
    settings.update({"radio_mix": "off"})
    ids = [t["id"] for t in client.get("/radio/dQw4w9WgXcQ").json()["tracks"]]
    assert ids and not any(i.startswith("K") for i in ids)


def test_no_kept_songs_no_change(client):
    assert [t["id"] for t in client.get("/radio/dQw4w9WgXcQ").json()["tracks"]] == ["9bZkp7q19f0"]


def test_kept_songs_of_the_same_artist_come_first(library):
    meta.remember("SEEDSEEDSEE", "Seed", "Rick Astley", "")
    _songs(library, 10, artist="Someone Else")
    _songs(library, 4, artist="Rick Astley", start=100)
    yt = [{"id": f"Y{i:010d}", "title": f"Y{i}", "artist": "x"} for i in range(8)]
    out = kept.blend(yt, "SEEDSEEDSEE", "Rick Astley", 8, "many", audio.IMPORT_DIR, random.Random(1))
    added = [t for t in out if t["id"].startswith("K")]
    assert len(added) == 4 and sum(t["artist"] == "Rick Astley" for t in added) == 2  # up to half are the seed's artist, the rest at random


def test_a_small_library_is_not_repeated_within_one_radio(library):
    _songs(library, 2)
    yt = [{"id": f"Y{i:010d}", "title": f"Y{i}", "artist": "x"} for i in range(8)]
    out = kept.blend(yt, "SEEDSEEDSEE", "", 8, "many", audio.IMPORT_DIR)
    assert len(out) == 8 and sum(t["id"].startswith("K") for t in out) == 2 and len({t["id"] for t in out}) == 8


def test_kept_songs_are_spread_over_the_list(library):
    _songs(library, 10)
    yt = [{"id": f"Y{i:010d}", "title": f"Y{i}", "artist": "x"} for i in range(40)]
    out = kept.blend(yt, "SEEDSEEDSEE", "", 40, "some", audio.IMPORT_DIR, random.Random(2))
    where = [i for i, t in enumerate(out) if t["id"].startswith("K")]
    assert len(where) == 10 and where[0] < 8 and where[-1] > 32 and all(b - a >= 2 for a, b in zip(where, where[1:]))


def test_a_deleted_copy_is_not_mixed_in(library):
    dest = _keep(library, "AAAAAAAAAAA", "Gone", "Band")
    dest.unlink()
    yt = [{"id": f"Y{i:010d}", "title": f"Y{i}", "artist": "x"} for i in range(8)]
    assert kept.blend(yt, "SEEDSEEDSEE", "", 8, "many", audio.IMPORT_DIR) == yt


def test_the_info_counts_kept_songs(client, library):
    _songs(library, 3)
    info = {i["label"]["en"]: i["value"] for i in client.get("/settings").json()["info"]}
    assert info["Songs kept in the library"] == "3"
