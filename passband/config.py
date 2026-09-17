"""Load YAML config + .env. No secrets or locations are hardcoded in source."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")


def _config_dir() -> Path:
    """Where the YAML lives. Resolved per call, never frozen at import.

    PASSBAND_CONFIG_DIR wins when set; otherwise the repo's own config/.
    Call-time resolution is what lets a test (or a future bind mount) point
    this somewhere else after the module has already been imported.
    """
    override = os.getenv("PASSBAND_CONFIG_DIR")
    return Path(override) if override else ROOT / "config"


def __getattr__(name: str):
    """Keep the old constant names working, but resolve them live (PEP 562).

    Plain module constants would go stale the moment the corresponding env var
    is set after import, which is exactly the bug this seam exists to prevent.
    DB_PATH/OUT_DIR are kept only for back-compat with an outside importer;
    in-tree
    callers use db_path()/out_dir() so the value is never frozen into a default
    argument.
    """
    if name == "CONFIG_DIR":
        return _config_dir()
    if name == "DB_PATH":
        return db_path()
    if name == "OUT_DIR":
        return out_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _load_yaml(name: str) -> dict:
    path = _config_dir() / name
    if not path.exists():  # optional configs (alerts.yaml) degrade to empty
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# --------------------------------------------------------------------------
# Location overrides
#
# The three values that must never sit in a public repo are env-overridable.
# Precedence is env > YAML > absent. A malformed env value NEVER raises and
# never breaks a send: it complains once and the YAML value stands.
# --------------------------------------------------------------------------

# (var name, raw value) pairs already complained about. Module-level so the
# warning survives config.cache_clear() — one bad env var, one log line, not
# one per call.
_WARNED: set[tuple[str, str]] = set()


def _warn_once(name: str, raw: str, reason: str,
               remedy: str = "using config file value") -> None:
    key = (name, raw)
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(f"config: ignoring malformed {name}={raw!r} ({reason}); {remedy}")


def _env_floats(name: str, count: int) -> list[float] | None:
    """Parse ``"1.5,-2.5"`` into floats. None means "leave the YAML alone"."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) != count:
        _warn_once(name, raw, f"expected {count} comma-separated numbers, got {len(parts)}")
        return None
    try:
        return [float(p) for p in parts]
    except ValueError:
        _warn_once(name, raw, "not all values are numbers")
        return None


def _env_terms(name: str) -> list[str] | None:
    """Parse ``"Dallas, Tarrant"`` into a stripped list. None means unset."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    terms = [t.strip() for t in raw.split(",") if t.strip()]
    if not terms:
        _warn_once(name, raw, "no terms after splitting on commas")
        return None
    return terms


def _apply_geo_env(geo: dict) -> dict:
    point = _env_floats("PASSBAND_FORECAST_POINT", 2)
    if point is not None:
        geo["forecast_point"] = point
    bbox = _env_floats("PASSBAND_BBOX", 4)
    if bbox is not None:
        geo["bbox"] = bbox
    return geo


def _ensure_dict(parent: dict, key: str) -> dict:
    """The nested block at ``key``, created when missing or not a mapping.

    A YAML key that is present but empty parses as None, and None.setdefault
    would raise at config load instead of at the point of use.
    """
    child = parent.get(key)
    if not isinstance(child, dict):
        child = parent[key] = {}
    return child


def _apply_alerts_env(alerts: dict) -> dict:
    """Overlay the two alerts.yaml lists that encode where the operator lives.

    Both live in alerts.yaml rather than geo.yaml because that is where the
    collectors read them from, and the code is what ships:

      structured.nws.local_terms -> nws_events, gates the `events_local` set
      watchlist.geo_terms        -> watchlist_hits, gates every `near: true` rule

    Both FAIL CLOSED. An empty list matches nothing rather than everything, so
    the shipped config can only carry placeholders because these overrides exist
    to backfill them. Dropping either override silently mutes an alert path
    while the newsletter keeps sending normally.
    """
    local = _env_terms("PASSBAND_LOCAL_TERMS")
    if local is not None:
        _ensure_dict(_ensure_dict(alerts, "structured"), "nws")["local_terms"] = local

    geo = _env_terms("PASSBAND_GEO_TERMS")
    if geo is not None:
        _ensure_dict(alerts, "watchlist")["geo_terms"] = geo

    return alerts


@lru_cache(maxsize=None)
def config() -> dict:
    """Merged config: yaml files + selected env vars."""
    return {
        "sources": _load_yaml("sources.yaml"),
        "sections": _load_yaml("sections.yaml").get("sections", []),
        "rotation": _load_yaml("sections.yaml").get("rotation", {}),
        # Optional brand strings. `or {}` rather than a default arg: a bare
        # `newsletter:` with nothing under it parses as None, and None.get()
        # would crash at send time.
        "newsletter": _load_yaml("sections.yaml").get("newsletter") or {},
        "geo": _apply_geo_env(_load_yaml("geo.yaml")),
        "llm": _load_yaml("llm.yaml"),
        "alerts": _apply_alerts_env(_load_yaml("alerts.yaml")),
        "env": {
            "freshrss_url": os.getenv("FRESHRSS_URL", ""),
            "freshrss_user": os.getenv("FRESHRSS_USER", ""),
            "freshrss_password": os.getenv("FRESHRSS_API_PASSWORD", ""),
            "litellm_base_url": os.getenv("LITELLM_BASE_URL", "http://localhost:4000"),
            "litellm_api_key": os.getenv("LITELLM_API_KEY", "sk-local"),
            "resend_api_key": os.getenv("RESEND_API_KEY", ""),
            "newsletter_from": os.getenv("NEWSLETTER_FROM", ""),
            "newsletter_to": os.getenv("NEWSLETTER_TO", ""),
        },
    }


def section_by_id(section_id: str) -> dict | None:
    for s in config()["sections"]:
        if s["id"] == section_id:
            return s
    return None


# --------------------------------------------------------------------------
# State locations
#
# Both overridable via env so cron can place the DB on fast local disk.
#
# An empty string is not a value. Orchestrators routinely write empty strings
# for declared-but-unfilled variables, and treating one as set would resolve the
# store to a fresh, empty database on deploy.
#
# Resolved per call, never frozen at import: the same seam as _config_dir().
# --------------------------------------------------------------------------

def _env_value(name: str) -> str | None:
    """The live value for an env var, or None if it is unset or blank.

    Stripped before the emptiness test: a whitespace-only value is not a value,
    and treating one as set resolves the store to a fresh, empty database.
    """
    raw = (os.getenv(name) or "").strip()
    return raw or None


def _env_path(name: str, default: Path) -> Path:
    raw = _env_value(name)
    return Path(raw) if raw else default


def _env_int(name: str, default: int) -> int:
    """Like _env_path, but never raises on a typo.

    int() on a bad value would propagate out of module scope in server.py and
    crash-loop the container instead of serving on the default port.
    """
    raw = _env_value(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        _warn_once(name, raw, "not an integer", f"using the default {default}")
        return default


def db_path() -> Path:
    return _env_path("PASSBAND_DB_PATH", ROOT / "store" / "passband.db")


def out_dir() -> Path:
    return _env_path("PASSBAND_OUT_DIR", ROOT / "out")


# --------------------------------------------------------------------------
# MCP server identity
#
# The name and the bind resolution live here rather than in server.py, because
# server.py imports
# mcp.server.fastmcp at module scope: keeping the name and the bind resolution
# in config means they are testable without the MCP stack installed.
# --------------------------------------------------------------------------

MCP_SERVER_NAME = "passband-news"


def mcp_host() -> str:
    return _env_value("PASSBAND_MCP_HOST") or "0.0.0.0"


def mcp_port() -> int:
    return _env_int("PASSBAND_MCP_PORT", 8000)


def mcp_pool_cap() -> int:
    """Cap on items pulled into memory by one tool call before ranking."""
    return _env_int("PASSBAND_MCP_POOL_CAP", 2000)
