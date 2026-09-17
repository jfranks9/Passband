"""Unit 1 — the config test seam.

CONFIG_DIR resolves from PASSBAND_CONFIG_DIR when set, else ROOT/"config".
Degradation is unchanged: a missing optional YAML yields {} and never raises.
"""
from __future__ import annotations

import passband.config
from passband.config import config


def test_config_dir_defaults_to_repo_config():
    """Absent env, the config dir is the repo's own config/ directory."""
    assert passband.config._config_dir() == passband.config.ROOT / "config"
    # The public name keeps working for any existing consumer.
    assert passband.config.CONFIG_DIR == passband.config.ROOT / "config"
    # And the seam really is what feeds _load_yaml: the shipped sources.yaml
    # is what config() returns when nothing overrides the directory.
    assert config()["sources"]


def test_config_dir_env_override_reads_alternate_dir(config_dir):
    """PASSBAND_CONFIG_DIR set *after* import must still be honoured."""
    config_dir.write("sources.yaml", {"freshrss": {"default_section": "sentinel"}})
    config_dir.write(
        "sections.yaml",
        {"sections": [{"id": "sentinel"}], "rotation": {"slots_per_day": 9}},
    )

    assert passband.config._config_dir() == config_dir.path
    assert passband.config.CONFIG_DIR == config_dir.path

    cfg = config()
    assert cfg["sources"]["freshrss"]["default_section"] == "sentinel"
    assert cfg["sections"] == [{"id": "sentinel"}]
    assert cfg["rotation"] == {"slots_per_day": 9}
    # The real repo config is not bleeding through.
    assert "category_map" not in cfg["sources"]["freshrss"]


def test_missing_optional_yaml_degrades_to_empty(config_dir):
    """Absent files yield {} (or [] for sections) with no raise."""
    assert not (config_dir.path / "alerts.yaml").exists()

    cfg = config()
    assert cfg["alerts"] == {}
    assert cfg["geo"] == {}
    assert cfg["llm"] == {}
    assert cfg["sources"] == {}
    assert cfg["sections"] == []
    assert cfg["rotation"] == {}

    # Present-but-empty degrades the same way (yaml.safe_load returns None).
    config_dir.write("alerts.yaml", "")
    assert config()["alerts"] == {}


def test_config_cache_is_cleared_between_tests(config_dir):
    """The autouse fixture actually resets the lru_cache; every later test
    in the suite depends on this being true."""
    # Earlier tests in this module called config(). If the fixture were not
    # clearing, these counters would be non-zero and their values stale.
    assert config.cache_info().hits == 0
    assert config.cache_info().misses == 0

    config_dir.write("geo.yaml", {"forecast_point": [1.5, 2.5]})

    first = config()
    assert config.cache_info().misses == 1
    assert first["geo"] == {"forecast_point": [1.5, 2.5]}

    # Still genuinely cached within a single test.
    assert config() is first
    assert config.cache_info().hits == 1


# ---------------------------------------------------------------------------
# Structured collector wiring
#
# usgs_events() reads its endpoint out of sources.yaml and returns [] when the
# lookup misses — no exception, no log line, no failed send. So a renamed block
# or a typo'd key silently disables earthquake alerting, and the only symptom is
# alerts that never arrive. These two tests are the only thing standing between
# that rename and a quiet outage.
# ---------------------------------------------------------------------------

RISK_BLOCK = "risk"
USGS_KEY = "usgs_earthquakes"


def test_shipped_sources_yaml_wires_the_usgs_collector():
    """The COMMITTED config must provide the URL the collector actually reads."""
    import yaml as _yaml

    from passband.config import ROOT

    shipped = _yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))
    block = shipped.get(RISK_BLOCK)
    assert isinstance(block, dict), (
        f"config/sources.yaml has no {RISK_BLOCK!r} block; "
        f"passband/collectors/alerts.py::usgs_events reads its endpoint from there "
        f"and returns [] silently when it is missing."
    )
    url = block.get(USGS_KEY)
    assert isinstance(url, str) and url.startswith("http"), (
        f"{RISK_BLOCK}.{USGS_KEY} is not a URL: {url!r}"
    )


def test_usgs_events_is_silent_when_unwired(config_dir):
    """Pins the fail-quiet behaviour the test above exists to compensate for.

    If this ever starts raising instead, that is an improvement — but it is a
    deliberate one, and this test should be updated rather than deleted.
    """
    from passband.collectors.alerts import usgs_events

    config_dir.write("sources.yaml", {"freshrss": {}})
    assert usgs_events({}, {}) == []
