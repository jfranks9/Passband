"""Unit 2 — location out of git.

Precedence is env > YAML > absent. A malformed env value must never raise and
must never break a send: it falls back to whatever the YAML said.

Note the coordinates used below are deliberately fictional. Asserting against
the operator's real forecast point would re-publish in ``tests/`` exactly the
value this unit is removing from ``config/``.
"""
from __future__ import annotations

import yaml

from passband import config as config_mod
from passband.config import config

# A stand-in for a user's committed geo.yaml. Numbers are nonsense on purpose.
YAML_GEO = {"bbox": [1.0, 2.0, 3.0, 4.0], "forecast_point": [5.5, 6.5]}


def _write_geo(config_dir, geo=None):
    config_dir.write("geo.yaml", YAML_GEO if geo is None else geo)


# ---------------------------------------------------------------------------
# forecast_point
# ---------------------------------------------------------------------------

def test_forecast_point_from_yaml_when_no_env(config_dir):
    """No env set: the YAML value is passed through untouched."""
    _write_geo(config_dir)
    assert config()["geo"]["forecast_point"] == [5.5, 6.5]


def test_forecast_point_env_overrides_yaml(config_dir, monkeypatch):
    """Env wins over YAML, parsed into two floats in the given order."""
    _write_geo(config_dir)
    monkeypatch.setenv("PASSBAND_FORECAST_POINT", "10.5,-20.25")
    config.cache_clear()
    assert config()["geo"]["forecast_point"] == [10.5, -20.25]


def test_forecast_point_malformed_env_falls_back_to_yaml(config_dir, monkeypatch):
    """Unparseable env never raises; the YAML value survives."""
    for bad in ("abc", "10.5,abc", ",", "   "):
        _write_geo(config_dir)
        monkeypatch.setenv("PASSBAND_FORECAST_POINT", bad)
        config.cache_clear()
        assert config()["geo"]["forecast_point"] == [5.5, 6.5], bad


def test_forecast_point_wrong_arity_falls_back(config_dir, monkeypatch):
    """One value or three values is not a point; fall back, do not guess."""
    for bad in ("10.5", "1,2,3"):
        _write_geo(config_dir)
        monkeypatch.setenv("PASSBAND_FORECAST_POINT", bad)
        config.cache_clear()
        assert config()["geo"]["forecast_point"] == [5.5, 6.5], bad


# ---------------------------------------------------------------------------
# bbox
# ---------------------------------------------------------------------------

def test_bbox_env_override_parses_four_floats(config_dir, monkeypatch):
    """Four floats, ordering preserved — [min_lon, min_lat, max_lon, max_lat]."""
    _write_geo(config_dir)
    monkeypatch.setenv("PASSBAND_BBOX", "-9.5, 1.25,-8.0,2.5")
    config.cache_clear()
    parsed = config()["geo"]["bbox"]
    assert parsed == [-9.5, 1.25, -8.0, 2.5]
    assert all(isinstance(v, float) for v in parsed)
    # Wrong arity falls back rather than truncating to the first four.
    monkeypatch.setenv("PASSBAND_BBOX", "-9.5,1.25,-8.0")
    config.cache_clear()
    assert config()["geo"]["bbox"] == [1.0, 2.0, 3.0, 4.0]


# ---------------------------------------------------------------------------
# local_terms
#
# local_terms is owned by alerts.yaml, not geo.yaml: the only reader is
# passband/collectors/alerts.py::nws_events, which takes it from
# acfg["structured"]["nws"]["local_terms"]. The release plan calls it a geo
# value; the code disagrees, and the code is what ships.
# ---------------------------------------------------------------------------

ALERTS_YAML = {"structured": {"nws": {"area": "XX", "local_terms": ["FromYaml"]}}}


def _nws(cfg):
    return cfg["alerts"]["structured"]["nws"]


def test_local_terms_env_override_splits_and_strips(config_dir, monkeypatch):
    """Comma separated, whitespace tolerated, empties dropped."""
    config_dir.write("alerts.yaml", ALERTS_YAML)
    assert _nws(config())["local_terms"] == ["FromYaml"]

    monkeypatch.setenv("PASSBAND_LOCAL_TERMS", "  Alpha , Bravo,Charlie  ")
    config.cache_clear()
    assert _nws(config())["local_terms"] == ["Alpha", "Bravo", "Charlie"]
    # Sibling keys in the same block are not disturbed by the overlay.
    assert _nws(config())["area"] == "XX"


# ---------------------------------------------------------------------------
# collector contract
# ---------------------------------------------------------------------------

def test_forecast_line_returns_none_when_point_absent(config_dir):
    """No forecast_point => no network call and no header line.

    The guard in forecast_line() runs before requests is ever touched, so this
    stays an offline test.
    """
    from passband.collectors.forecast import forecast_line

    config_dir.write("geo.yaml", {"bbox": [1.0, 2.0, 3.0, 4.0]})
    assert forecast_line() is None

    config_dir.write("geo.yaml", {"forecast_point": []})
    assert forecast_line() is None

    config_dir.remove("geo.yaml")
    assert forecast_line() is None


# ---------------------------------------------------------------------------
# The point of the unit
# ---------------------------------------------------------------------------

def _numeric_leaves(value) -> list:
    """Every number reachable from ``value``, including numeric strings.

    Numeric strings count: quoting a coordinate does not make it a placeholder,
    and a test that only rejected ints and floats would wave through
    ``bbox: ["-11.1", "22.2", "-33.3", "44.4"]``.
    """
    if isinstance(value, bool):  # bools are ints in Python; not coordinates
        return []
    if isinstance(value, (int, float)):
        return [value]
    if isinstance(value, dict):
        return [n for v in value.values() for n in _numeric_leaves(v)]
    if isinstance(value, (list, tuple)):
        return [n for v in value for n in _numeric_leaves(v)]
    if isinstance(value, str):
        try:
            return [float(value)]
        except ValueError:
            return []
    return []


def test_shipped_geo_yaml_contains_no_real_coordinates():
    """The committed config/geo.yaml must not carry anyone's location.

    Deliberately reads the real repo file rather than a fixture — a fixture
    would prove nothing about what gets pushed. Asserts on the *parsed* values
    so a future edit cannot slip a coordinate past by reformatting, quoting it,
    or nesting it.
    """
    shipped = config_mod.ROOT / "config" / "geo.yaml"
    assert shipped.is_file(), f"expected a committed geo.yaml at {shipped}"

    doc = yaml.safe_load(shipped.read_text(encoding="utf-8")) or {}

    for key in ("forecast_point", "bbox"):
        found = _numeric_leaves(doc.get(key))
        assert not found, (
            f"config/geo.yaml ships concrete coordinates in {key!r}: {found}. "
            f"Real values belong in PASSBAND_FORECAST_POINT / PASSBAND_BBOX, "
            f"not in a public repo."
        )


# ---------------------------------------------------------------------------
# geo_terms
#
# geo_terms lives under `watchlist:` in alerts.yaml and gates every rule marked
# `near: true`. It FAILS CLOSED: watchlist_hits skips a near-rule when no term
# matches, so an empty list matches NOTHING rather than everything.
#
# That is the whole reason this override exists. local_terms could be blanked in
# the shipped config because PASSBAND_LOCAL_TERMS backfills it; geo_terms had no
# such backfill, so placeholdering it would have silently muted the watchlist —
# including the tropical rule the alert layer was built for.
# ---------------------------------------------------------------------------

WATCHLIST_YAML = {
    "watchlist": {
        "lookback_hours": 30,
        "geo_terms": ["fromyaml"],
        "rules": [{"name": "tropical", "any": ["tropical storm"], "near": True}],
    }
}


def _watchlist(cfg):
    return cfg["alerts"]["watchlist"]


def test_geo_terms_from_yaml_when_no_env(config_dir):
    config_dir.write("alerts.yaml", WATCHLIST_YAML)
    assert _watchlist(config())["geo_terms"] == ["fromyaml"]


def test_geo_terms_env_override_splits_and_strips(config_dir, monkeypatch):
    """Same contract as local_terms: comma separated, whitespace tolerated."""
    config_dir.write("alerts.yaml", WATCHLIST_YAML)
    assert _watchlist(config())["geo_terms"] == ["fromyaml"]

    monkeypatch.setenv("PASSBAND_GEO_TERMS", "  Alpha , Bravo,Charlie  ")
    config.cache_clear()
    assert _watchlist(config())["geo_terms"] == ["Alpha", "Bravo", "Charlie"]
    # The overlay must not disturb siblings — rules and lookback are load-bearing.
    assert _watchlist(config())["lookback_hours"] == 30
    assert _watchlist(config())["rules"][0]["name"] == "tropical"


def test_geo_terms_overlay_creates_watchlist_block_when_absent(config_dir, monkeypatch):
    """A config with no watchlist: block must not crash the overlay."""
    config_dir.write("alerts.yaml", {"structured": {}})
    monkeypatch.setenv("PASSBAND_GEO_TERMS", "Alpha")
    config.cache_clear()
    assert _watchlist(config())["geo_terms"] == ["Alpha"]


def _conn_with_item(title: str):
    import sqlite3
    import time as _time

    from passband import store

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(store.SCHEMA)
    now = int(_time.time())
    conn.execute(
        "INSERT INTO items (content_hash, section, title, url, summary,"
        " source_title, source_id, published, fetched_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        ("h1", "global", title, "https://example.invalid/1", "", "Src", "s1", now, now),
    )
    return conn


def test_near_rules_are_muted_by_empty_geo_terms():
    """Pins the fail-closed behaviour that makes PASSBAND_GEO_TERMS necessary.

    If someone later 'fixes' the empty case to mean 'no geo restriction', this
    fails — and that would be a real decision, not a tidy-up, because it changes
    what a mis-set deployment does: silence versus every near-rule firing
    worldwide.
    """
    from passband.collectors.alerts import watchlist_hits

    rule = {"name": "tropical", "any": ["tropical storm"], "near": True}
    conn = _conn_with_item("Tropical storm nears the Texas coast")

    populated = {"watchlist": {"lookback_hours": 30, "geo_terms": ["texas"],
                               "rules": [rule]}}
    assert len(watchlist_hits(conn, populated)) == 1

    blank = {"watchlist": {"lookback_hours": 30, "geo_terms": [], "rules": [rule]}}
    assert watchlist_hits(conn, blank) == []
