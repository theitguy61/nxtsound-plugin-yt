"""The admin's settings: prefetch count, keeping songs in the library, yt-dlp update schedule and branch."""
import asyncio
import json
import os
import time
from pathlib import Path

import pytest

import audio
import settings
from tests.conftest import AUDIO, REAL_DOWNLOAD
from tests.vendor import pluginproto


@pytest.fixture(autouse=True)
def fresh_settings(tmp_path, monkeypatch):
    """Every test starts with the defaults and its own settings file and import folder."""
    monkeypatch.setattr(settings, "FILE", tmp_path / "settings.json")
    monkeypatch.setattr(audio, "IMPORT_DIR", tmp_path / "imported")
    settings.values.clear()
    settings.values.update(settings.DEFAULTS)
    yield
    settings.values.clear()
    settings.values.update(settings.DEFAULTS)


def test_schema_in_the_manifest_is_valid_and_complete(client):
    m = pluginproto.validate_manifest(client.get("/manifest").json())
    assert [s["key"] for s in m["settings"]] == ["prefetch", "storage", "radio_mix", "auto_update", "update_hours", "channel"]
    by = {s["key"]: s for s in m["settings"]}
    assert by["update_hours"]["depends_on"] == "auto_update" and by["channel"]["depends_on"] == "auto_update"
    assert {c["value"] for c in by["storage"]["choices"]} == {"cache", "library"} and {c["value"] for c in by["channel"]["choices"]} == {"stable", "nightly"}
    assert settings.DEFAULTS == {"prefetch": 2, "storage": "cache", "radio_mix": "some", "auto_update": True, "update_hours": 6, "channel": "stable"}
    assert all(isinstance(s["label"], dict) and "de" in s["label"] for s in m["settings"])  # German texts for the admin area


def test_put_settings_applies_validates_and_remembers(client, tmp_path):
    r = client.put("/settings", json={"values": {"prefetch": 5, "storage": "library", "channel": "nightly", "update_hours": 12, "auto_update": False}})
    assert r.status_code == 200 and {i["label"]["en"] for i in r.json()["info"]} >= {"yt-dlp version", "Cache"}
    assert settings.values == {"prefetch": 5, "storage": "library", "radio_mix": "some", "channel": "nightly", "update_hours": 12, "auto_update": False}
    assert json.loads((tmp_path / "settings.json").read_text())["prefetch"] == 5
    client.put("/settings", json={"values": {"prefetch": 99, "storage": "tape", "auto_update": "yes", "nope": 1}})  # invalid values and unknown keys are ignored
    assert settings.values["prefetch"] == 5 and settings.values["storage"] == "library" and settings.values["auto_update"] is False
    assert client.put("/settings", json={"nope": 1}).status_code == 400
    settings.values.update(settings.DEFAULTS)
    settings.load()  # a restart: the remembered values come back
    assert settings.get("prefetch") == 5
    assert client.get("/settings").json()["info"]


def test_settings_need_the_token(anon):
    assert anon.put("/settings", json={"values": {}}).status_code == 401
    assert anon.get("/settings").status_code == 401
    assert anon.post("/prefetch", json={"ids": []}).status_code == 401


def test_environment_settings_win_at_start(monkeypatch):
    monkeypatch.setenv("PLUGIN_SETTINGS", json.dumps({"prefetch": 7, "channel": "nightly"}))
    settings.load()
    assert settings.get("prefetch") == 7 and settings.get("channel") == "nightly" and settings.get("storage") == "cache"
    monkeypatch.setenv("PLUGIN_SETTINGS", "{broken")
    settings.load()  # garbage is ignored, defaults stay
    assert settings.get("prefetch") == 2


# ---------- Fetching ahead ----------

def test_prefetch_fetches_as_many_as_allowed_in_the_background(client, fakes):
    ids = ["AAAAAAAAAA1", "AAAAAAAAAA2", "AAAAAAAAAA3", "AAAAAAAAAA4"]
    settings.update({"prefetch": 2})
    r = client.post("/prefetch", json={"ids": ids + ["not-an-id"]})
    assert r.status_code == 202 and r.json() == {"accepted": 2}
    deadline = time.time() + 5
    while time.time() < deadline and len(fakes) < 2:
        time.sleep(0.05)
    assert sorted(fakes) == ["AAAAAAAAAA1", "AAAAAAAAAA2"]
    assert client.get("/stream/AAAAAAAAAA1").content == AUDIO and fakes.count("AAAAAAAAAA1") == 1  # served from what was fetched ahead
    assert client.post("/prefetch", json={"ids": ids}).json() == {"accepted": 0}  # the next two are already there; the window counts from the start of the list


def test_prefetch_zero_does_nothing(client, fakes):
    settings.update({"prefetch": 0})
    assert client.post("/prefetch", json={"ids": ["AAAAAAAAAA1"]}).json() == {"accepted": 0}
    assert fakes == []


def test_prefetch_failures_do_not_break_anything(client, fakes):
    assert client.post("/prefetch", json={"ids": ["FAILFAILFAI"]}).json() == {"accepted": 1}
    time.sleep(0.3)
    assert client.get("/manifest").status_code == 200
    assert client.post("/prefetch", json={"ids": ["x"] * 21}).status_code == 422


# ---------- Keeping songs in the library ----------

INFO = {"track": "Never Gonna Give You Up", "artists": ["Rick Astley"], "album": "Whenever You Need Somebody", "title": "Rick Astley - Never Gonna Give You Up (Official Video)"}


def test_nothing_is_kept_in_cache_mode(tmp_path):
    src = tmp_path / "x.m4a"
    src.write_bytes(AUDIO)
    assert audio.keep(INFO, src) is None and not (tmp_path / "imported").exists()


def test_library_mode_copies_into_artist_album_folders(tmp_path):
    settings.update({"storage": "library"})
    src = tmp_path / "x.m4a"
    src.write_bytes(AUDIO)
    dest = audio.keep(INFO, src)
    assert dest == tmp_path / "imported" / "Rick Astley" / "Whenever You Need Somebody" / "Never Gonna Give You Up.m4a" and dest.read_bytes() == AUDIO
    assert audio.keep(INFO, src) is None  # once per song
    assert not [p for p in (tmp_path / "imported").rglob("*") if p.name.endswith(".part")]  # no leftovers; unfinished files are hidden by the dot anyway


def test_the_picture_is_saved_next_to_the_kept_song(tmp_path, monkeypatch):
    settings.update({"storage": "library"})
    monkeypatch.setattr(audio, "_fetch", lambda url: b"IMG")
    src = tmp_path / "x.m4a"
    src.write_bytes(AUDIO)
    dest = audio.keep({**INFO, "thumbnail": "https://i.ytimg.com/vi/x/maxresdefault.jpg?sqp=1"}, src)
    assert dest.with_suffix(".jpg").read_bytes() == b"IMG"


def test_a_missing_or_unreachable_picture_does_not_matter(tmp_path, monkeypatch):
    settings.update({"storage": "library"})
    monkeypatch.setattr(audio, "_fetch", lambda url: (_ for _ in ()).throw(OSError("offline")))
    src = tmp_path / "x.m4a"
    src.write_bytes(AUDIO)
    dest = audio.keep({**INFO, "thumbnail": "https://i.ytimg.com/vi/x/a.jpg"}, src)
    assert dest.is_file() and not dest.with_suffix(".jpg").exists()
    src2 = tmp_path / "y.m4a"
    src2.write_bytes(AUDIO)
    assert audio.keep({**INFO, "track": "Other", "thumbnail": "http://insecure/a.jpg"}, src2).is_file()  # only https is fetched


def test_songs_without_album_or_artist_get_flat_names(tmp_path):
    settings.update({"storage": "library"})
    src = tmp_path / "x.webm"
    src.write_bytes(AUDIO)
    assert audio.keep({"track": "Song", "artist": "A, B"}, src).name == "A - Song.webm"
    assert audio.keep({"title": "Some Band - Their Song", "uploader": "Some Channel"}, src).name == "Some Band - Their Song.webm"
    assert audio.keep({"title": "Just a title", "uploader": "Artist - Topic"}, src).name == "Artist - Just a title.webm"
    assert audio.keep({"title": "Only a title"}, src).name == "Only a title.webm"
    assert audio.keep({}, src) is None


def test_file_names_are_made_safe(tmp_path):
    settings.update({"storage": "library"})
    src = tmp_path / "x.m4a"
    src.write_bytes(AUDIO)
    dest = audio.keep({"track": 'AC/DC: "Back in Black"?', "artists": ["../../evil"], "album": "A|B"}, src)
    assert tmp_path / "imported" in dest.parents and ".." not in dest.relative_to(tmp_path / "imported").parts
    assert not any(c in dest.name for c in '/:*?"<>|')


def test_download_keeps_a_copy_in_library_mode(tmp_path, monkeypatch):
    """The real download path: yt-dlp returns the track info, the file lands in the cache and is copied into the import folder."""
    monkeypatch.setattr(audio, "CACHE_DIR", tmp_path / "cache")
    settings.update({"storage": "library"})

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def extract_info(self, url, download):
            (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
            (tmp_path / "cache" / "dQw4w9WgXcQ.m4a").write_bytes(AUDIO)
            return INFO

    monkeypatch.setattr(audio.yt_dlp, "YoutubeDL", FakeYDL)
    p = REAL_DOWNLOAD("dQw4w9WgXcQ", __import__("threading").Event())
    assert p.read_bytes() == AUDIO
    assert (tmp_path / "imported" / "Rick Astley" / "Whenever You Need Somebody" / "Never Gonna Give You Up.m4a").is_file()


def test_a_failing_copy_never_breaks_playback(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "CACHE_DIR", tmp_path / "cache")
    settings.update({"storage": "library"})
    monkeypatch.setattr(audio, "keep", lambda info, p: (_ for _ in ()).throw(OSError("read-only file system")))

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def extract_info(self, url, download):
            (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
            (tmp_path / "cache" / "dQw4w9WgXcQ.m4a").write_bytes(AUDIO)
            return INFO

    monkeypatch.setattr(audio.yt_dlp, "YoutubeDL", FakeYDL)
    assert REAL_DOWNLOAD("dQw4w9WgXcQ", __import__("threading").Event()).is_file()


# ---------- yt-dlp updates ----------

def run_loop(monkeypatch, steps, auto=True, hours=6, channel="stable"):
    """Runs update_loop; `steps` is called while it waits, to change settings."""
    settings.update({"auto_update": auto, "update_hours": hours, "channel": channel})
    ran, waits = [], []
    monkeypatch.setattr(audio, "update_sync", lambda: ran.append(settings.get("channel")))
    monkeypatch.setattr(audio, "_installed_version", lambda: "2026.1.1")
    real = asyncio.wait_for

    async def spy(aw, timeout):
        waits.append(timeout)
        return await real(aw, 5)  # the test cancels the loop long before; the real timeout is only recorded

    monkeypatch.setattr(asyncio, "wait_for", spy)

    async def main():
        audio.ready = asyncio.Event()
        task = asyncio.create_task(audio.update_loop())
        await asyncio.sleep(0.05)
        await steps()
        await asyncio.sleep(0.4)
        task.cancel()

    asyncio.run(main())
    return ran, waits


def test_updates_run_at_start_and_follow_the_schedule(monkeypatch):
    async def nothing(): pass
    ran, waits = run_loop(monkeypatch, nothing, hours=12)
    assert ran and waits[0] == 12 * 3600


def test_updates_off_means_no_pip_and_no_schedule(monkeypatch):
    async def nothing(): pass
    ran, waits = run_loop(monkeypatch, nothing, auto=False)
    assert ran == [] and waits[0] is None  # waits only for a change of settings
    assert audio.state["version"] == "2026.1.1"  # the installed version is still reported


def test_turning_updates_on_or_switching_branch_checks_at_once(monkeypatch):
    async def turn_on():
        settings.update({"auto_update": True, "channel": "nightly"})
    ran, waits = run_loop(monkeypatch, turn_on, auto=False)
    assert ran == ["nightly"]  # one immediate check, with the new branch


def test_changing_unrelated_settings_does_not_trigger_a_check(monkeypatch):
    async def other():
        settings.update({"prefetch": 4, "storage": "library"})
    ran, _ = run_loop(monkeypatch, other)
    assert len(ran) == 1  # only the one at start


def test_stable_replaces_a_nightly_but_nightly_just_updates(monkeypatch):
    cmds = []

    class R:
        returncode, stderr = 0, ""

    monkeypatch.setattr(audio.subprocess, "run", lambda cmd, **kw: cmds.append(cmd) or R())
    monkeypatch.setattr(audio, "_installed_version", lambda: "2026.10.19.232824.dev0")
    audio._pip_update("stable")
    assert "--force-reinstall" in cmds[-1] and "--pre" not in cmds[-1]
    audio._pip_update("nightly")
    assert "--pre" in cmds[-1] and "--force-reinstall" not in cmds[-1]
    monkeypatch.setattr(audio, "_installed_version", lambda: "2026.10.1")
    audio._pip_update("stable")
    assert "--force-reinstall" not in cmds[-1] and "--pre" not in cmds[-1]  # stable on stable: a normal upgrade


def test_names_known_from_search_win_over_the_download_info(client, tmp_path):
    """The search results know artist, title and album cleanly; yt-dlp only has the video title."""
    client.get("/search", params={"q": "never"})  # shows (and so remembers) the track
    settings.update({"storage": "library"})
    src = tmp_path / "x.m4a"
    src.write_bytes(AUDIO)
    dest = audio.keep({"id": "dQw4w9WgXcQ", "title": "Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)", "uploader": "Rick Astley"}, src)
    assert dest == tmp_path / "imported" / "Rick Astley" / "Whenever You Need Somebody" / "Never Gonna Give You Up.m4a"


def test_album_pages_are_remembered_with_their_album(client):
    import meta
    client.get("/album/MPREb_abcdefghijk")
    assert meta.lookup("dQw4w9WgXcQ")["album"] == "Whenever You Need Somebody"
