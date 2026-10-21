"""Plugin protocol v1: constants and manifest validation (no dependency on the rest of the server).

Used by the core (plugins.py) and the checker (plugincheck.py); plugins may copy this file together with plugincheck.py
into their tests. See PLUGINS.md.
"""
import re

API_VERSION = 1  # version of the plugin protocol
ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
FEATURES = {"search", "stream", "radio", "artist", "album", "suggest", "prefetch"}  # suggest: search suggestions while typing; prefetch: hint about upcoming tracks (both optional)
KINDS = {"songs", "artists", "albums"}
SETTING_TYPES = {"bool", "int", "choice"}
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
LANGS = ("en", "de")  # languages a plugin may provide texts for (the first is the fallback)


def translate(msg: str, **values) -> str:
    """Hook for message translation. The server replaces it with its translator; standalone it just fills in the values."""
    return msg.format(**values) if values else msg


class PluginError(Exception):
    """Plugin unreachable, or its answer does not follow the protocol.

    `msg` is an English template with {placeholders}; `localized()` translates it into the language of the current request.
    """

    def __init__(self, msg: str, **values):
        super().__init__(msg.format(**values) if values else msg)
        self.msg, self.values = msg, values

    def localized(self) -> str:
        return translate(self.msg, **self.values)


def text(value, lang: str = "en") -> str:
    """A plugin text in the wanted language. Plugins give either a plain (English) string or {"en": ..., "de": ...}."""
    if isinstance(value, dict):
        return str(value.get(lang) or value.get("en") or next(iter(value.values()), ""))
    return str(value or "")


def _check_text(value, where: str, required: bool = True) -> None:
    ok = isinstance(value, str) and value.strip() or isinstance(value, dict) and value and all(
        isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in value.items()) and "en" in value
    if not ok and (required or value not in (None, "")):
        raise PluginError("Invalid manifest: {where} must be a text, or one text per language ('en' and optionally 'de').", where=where)


def require_identity(m: dict, where: str) -> None:
    """name, description and version are mandatory: the server shows all three in the admin area."""
    for key in ("name", "description", "version"):
        if not isinstance(m.get(key), str) or not m[key].strip():
            raise PluginError("{where}: '{key}' is required (a non-empty text).", where=where, key=key)


def validate_settings_schema(raw) -> list[dict]:
    """Checks the optional `settings` list of a manifest and returns it normalized.

    Every entry: key, type (bool | int | choice), label, default, optional help; int: min and max, optional unit;
    choice: choices [{value, label}]; optional depends_on (key of a bool setting: shown only while that one is on).
    """
    if raw in (None, []):
        return []
    if not isinstance(raw, list) or len(raw) > 30:
        raise PluginError("Invalid manifest: 'settings' must be a list of at most 30 entries.")
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not KEY_RE.match(item["key"]):
            raise PluginError("Invalid manifest: every setting needs a 'key' (a-z, 0-9, underscore).")
        key = item["key"]
        if any(o["key"] == key for o in out):
            raise PluginError("Invalid manifest: setting '{key}' is defined twice.", key=key)
        kind = item.get("type")
        if kind not in SETTING_TYPES:
            raise PluginError("Invalid manifest: setting '{key}' has an unknown type (allowed: {allowed}).", key=key, allowed=sorted(SETTING_TYPES))
        _check_text(item.get("label"), f"label of setting '{key}'")
        _check_text(item.get("help"), f"help of setting '{key}'", required=False)
        entry = {"key": key, "type": kind, "label": item["label"], "help": item.get("help") or ""}
        if kind == "int":
            lo, hi = item.get("min"), item.get("max")
            if not all(isinstance(x, int) and not isinstance(x, bool) for x in (lo, hi)) or lo > hi:
                raise PluginError("Invalid manifest: setting '{key}' needs integer 'min' and 'max'.", key=key)
            entry.update(min=lo, max=hi)
            if item.get("unit"):
                _check_text(item["unit"], f"unit of setting '{key}'")
                entry["unit"] = item["unit"]
        elif kind == "choice":
            choices = item.get("choices")
            if not isinstance(choices, list) or not choices or not all(isinstance(c, dict) and isinstance(c.get("value"), str) and c["value"] for c in choices):
                raise PluginError("Invalid manifest: setting '{key}' needs 'choices' with a 'value' and a 'label' each.", key=key)
            for c in choices:
                _check_text(c.get("label"), f"choice label of setting '{key}'")
            entry["choices"] = [{"value": c["value"], "label": c["label"]} for c in choices]
        entry["default"] = _coerce(entry, item.get("default"), strict=True)
        if item.get("depends_on"):
            dep = next((o for o in out if o["key"] == item["depends_on"]), None)
            if not dep or dep["type"] != "bool":
                raise PluginError("Invalid manifest: 'depends_on' of setting '{key}' must name an earlier bool setting.", key=key)
            entry["depends_on"] = dep["key"]
        out.append(entry)
    return out


def _coerce(entry: dict, value, strict: bool):
    """Value as the setting's type. strict: wrong input raises, otherwise the default is used."""
    ok, result = True, None
    if entry["type"] == "bool":
        ok, result = isinstance(value, bool), value
    elif entry["type"] == "int":
        ok = isinstance(value, int) and not isinstance(value, bool) and entry["min"] <= value <= entry["max"]
        result = value
    else:
        ok, result = value in [c["value"] for c in entry["choices"]], value
    if ok:
        return result
    if strict:
        raise PluginError("Invalid value for setting '{key}'.", key=entry["key"])
    return entry["default"]


def clean_values(schema: list[dict], values) -> dict:
    """All settings of the schema with valid values (unknown keys dropped, missing or invalid ones replaced by the default)."""
    values = values if isinstance(values, dict) else {}
    return {e["key"]: _coerce(e, values.get(e["key"]), strict=False) for e in schema}


def check_values(schema: list[dict], values) -> dict:
    """Like clean_values, but for input from the admin: an invalid value is an error (missing keys keep the default)."""
    if not isinstance(values, dict):
        raise PluginError("Invalid value for setting '{key}'.", key="")
    unknown = [k for k in values if k not in {e["key"] for e in schema}]
    if unknown:
        raise PluginError("Unknown setting '{key}'.", key=unknown[0])
    return {e["key"]: _coerce(e, values[e["key"]], strict=True) if e["key"] in values else e["default"] for e in schema}


def validate_manifest(m: dict) -> dict:
    if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not ID_RE.match(m["id"]):
        raise PluginError("Invalid manifest: 'id' is missing or invalid (a-z, 0-9, hyphen, 2-24 characters).")
    if m["id"] == "local":
        raise PluginError("The identifier 'local' is reserved for the own library.")
    if m.get("api_version") != API_VERSION:
        raise PluginError("The plugin speaks protocol version {got}, this server understands {want}.", got=m.get("api_version"), want=API_VERSION)
    features = m.get("features")
    if not isinstance(features, list) or not set(features) <= FEATURES or not features:
        raise PluginError("Invalid manifest: 'features' must be a non-empty list from {allowed}.", allowed=sorted(FEATURES))
    kinds = m.get("search_kinds", ["songs"])
    if not isinstance(kinds, list) or not set(kinds) <= KINDS:
        raise PluginError("Invalid manifest: 'search_kinds'.")
    require_identity(m, "Manifest")
    return {"id": m["id"], "name": m["name"].strip()[:60], "version": m["version"].strip()[:40],
            "description": m["description"].strip()[:300], "features": sorted(features), "search_kinds": sorted(kinds),
            "settings": validate_settings_schema(m.get("settings"))}
