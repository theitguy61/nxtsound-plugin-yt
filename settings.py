"""The plugin's settings. The admin sets them in the server (the schema is `settings` in plugin.json).

The server hands them over at start (environment variable PLUGIN_SETTINGS) and after every change (PUT /settings). The last
values are also kept in the data folder, so a plugin that runs on its own (without the server starting it) still has them after a restart.
"""
import json
import logging
import os
from pathlib import Path
from typing import Callable

log = logging.getLogger("settings")

SPEC = json.loads((Path(__file__).parent / "plugin.json").read_text(encoding="utf-8"))
SCHEMA = {e["key"]: e for e in SPEC.get("settings", [])}
DEFAULTS = {k: e["default"] for k, e in SCHEMA.items()}
FILE = Path(os.getenv("PLUGIN_DATA_DIR", Path(__file__).parent / "data")) / "settings.json"

values: dict = dict(DEFAULTS)
_listeners: list[Callable[[set], None]] = []


def _valid(entry: dict, v) -> bool:
    if entry["type"] == "bool":
        return isinstance(v, bool)
    if entry["type"] == "int":
        return isinstance(v, int) and not isinstance(v, bool) and entry["min"] <= v <= entry["max"]
    return v in [c["value"] for c in entry["choices"]]


def _merge(raw) -> set:
    """Takes over the valid values from `raw` (unknown keys and invalid values are ignored); returns the keys that changed."""
    changed = set()
    for key, v in (raw if isinstance(raw, dict) else {}).items():
        if key in SCHEMA and _valid(SCHEMA[key], v) and values[key] != v:
            values[key] = v
            changed.add(key)
    return changed


def load() -> None:
    values.update(DEFAULTS)
    try:
        _merge(json.loads(FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    try:
        _merge(json.loads(os.getenv("PLUGIN_SETTINGS", "{}")))
    except ValueError:
        log.warning("PLUGIN_SETTINGS is not valid JSON, ignored")


def get(key: str):
    return values[key]


def update(raw) -> set:
    """New values from the server; remembers them and tells the listeners which keys changed."""
    changed = _merge(raw)
    if changed:
        try:
            FILE.parent.mkdir(parents=True, exist_ok=True)
            FILE.write_text(json.dumps(values), encoding="utf-8")
        except OSError as e:
            log.warning("Settings could not be saved: %s", e)
        for cb in list(_listeners):
            cb(changed)
    return changed


def on_change(cb: Callable[[set], None]) -> None:
    _listeners.append(cb)


load()
