# yt: a YouTube Music source plugin

A small HTTP service that lets a self-hosted music server search and play YouTube Music. It speaks plugin protocol v1 and is built for
[NxtSound](https://github.com/nxtsound/nxtsound-server). It is a standalone project and not part of NxtSound itself.

Disclaimer: this project has nothing to do with YouTube or Google. Using YouTube content outside their own apps may violate their terms of service
and the law where you live. Use it for yourself and at your own risk. It relies on how YouTube behaves today and can stop working without notice.

## What you get

- Search with suggestions, radio from a song, artist pages and album pages
- Playback through yt-dlp, with a size-limited cache (oldest songs go first)
- Loading ahead: the next songs of the queue are fetched while the current one plays
- Optional: keep every downloaded song as `Artist/Album/Title.ext` in a folder of the plugin. A kept song always plays from there, without asking YouTube again, and the radio mixes kept songs in between YouTube's suggestions
- yt-dlp updates itself on a schedule, stable or nightly

## Requirements

- A server with plugin protocol v1 support, for example NxtSound
- Python 3.12 or newer, `git` and internet access on the machine that runs the plugin
- ffmpeg on that machine (the Docker image of NxtSound ships it)
- The first start downloads yt-dlp, ytmusicapi and Deno into the plugin's own environment, which takes a few minutes

## Install on NxtSound

In the admin area open *Plugins*, paste `https://github.com/theitguy61/nxtsound-plugin-yt` and press *Install*.

Or by hand, in the server's plugin folder:

```sh
git clone https://github.com/theitguy61/nxtsound-plugin-yt yt
```

then restart the server. The folder has to be called `yt`, it must equal the `id` in `plugin.json`.

## Settings

Everything is set in the server (gear button of the plugin), changes apply at once:

| Setting | Default | What it does |
|---|---|---|
| Songs to load ahead | 2 | Fetches the next songs of the queue early, 0 = only when played |
| Downloaded songs | Cache only | *Library* additionally saves each song for good (needs a writable music or import folder on the server) |
| Kept songs in the radio | Some | Mixes songs kept in the library into a radio: *Some* about every 4th, *Many* about every 2nd, *Off* none. Songs of the same artist come first; there are never more of them than YouTube's own |
| Update yt-dlp automatically | on | YouTube breaks yt-dlp regularly, updating keeps playback working |
| Check for updates every | 6 h | Only while automatic updates are on |
| yt-dlp branch | stable | *Nightly* gets YouTube fixes earlier but is less tested |

The cache size is limited by `CACHE_LIMIT_GB` (default 20).

### Kept songs

With *Also keep in the library* every song that is downloaded is copied into the library, and the plugin remembers which YouTube song it belongs to
(`kept.json` in its data folder). From then on:

- playing that song uses the copy: no download, nothing asked from YouTube, and the cache does not hold it a second time. This works for everyone who
  plays it through the plugin, whoever heard it first;
- if the copy is deleted from the library, the plugin forgets it and downloads the song again the next time;
- songs that were kept with an older version are found when they are played again (the search must have shown them since the plugin started).

The plugin only knows songs it kept itself. Music you put into the library by hand is not mixed into the radio by the plugin.

## Run it on its own

For a plugin on another machine:

```sh
cp env.example .env        # set PLUGIN_TOKEN, e.g. openssl rand -hex 24
docker compose up -d --build
```

Then register the address (`http://youtube-plugin:8100`) and the token in the server as a remote plugin. The compose file joins the Docker network of the
server; set `SERVER_NETWORK` if its name differs.

## When playback stops working

Almost always YouTube changed something. yt-dlp usually has a fix within days: leave automatic updates on or switch to nightly.
`GET /status` (send the token) shows the installed yt-dlp version and the last error.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q                      # no network, YouTube is replaced by stand-ins
PLUGIN_TOKEN=secret .venv/bin/uvicorn app:app --port 8100
.venv/bin/python -m tests.vendor.plugincheck http://localhost:8100 --token secret --query "radiohead"
```

| File | Role |
|---|---|
| `app.py` | HTTP interface of the protocol |
| `ytsource.py` | YouTube Music through ytmusicapi, mapped to protocol shapes |
| `audio.py` | Downloads, cache, loading ahead, keeping songs, yt-dlp updates |
| `kept.py` | The list of songs kept in the library: where each copy is, and the mix of kept songs into a radio |
| `settings.py`, `plugin.json` | Settings schema and the plugin's identity |
| `meta.py` | Remembers title, artist and album of shown songs |
| `tests/vendor/` | Copy of the protocol checker of NxtSound; refresh it when the protocol changes |

The protocol is described in [PLUGINS.md](https://github.com/nxtsound/nxtsound-server/blob/main/PLUGINS.md).

## License

MIT, see [LICENSE](LICENSE). Issues and pull requests are welcome, see [CONTRIBUTING.md](CONTRIBUTING.md).
