"""Shared pytest fixtures.

Three guarantees this file exists to provide:

1. ``config()`` is ``@lru_cache``'d, so without an explicit reset one test's
   config leaks into the next. Cleared before *and* after every test.
2. ``passband/config.py`` calls ``load_dotenv(ROOT / ".env")`` at import time, which
   happens during collection — before any fixture runs. So a developer's real
   ``.env`` is already in ``os.environ`` by the time tests start. We *delete*
   the leaky keys rather than merely asserting they are absent.
3. Tests never read the developer's real ``config/`` directory unless they
   explicitly ask for it; ``config_dir`` points ``PASSBAND_CONFIG_DIR`` at a
   throwaway tmp directory.

No test in this suite may touch the network.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from passband.config import config

# Anything matching these prefixes is scrubbed from os.environ for every test.
# FRESHRSS_/RESEND_/NEWSLETTER_ are credentials and delivery targets.
# LITELLM_ is included because an unscrubbed LITELLM_BASE_URL is the one value
# that could turn a curate() test into a live network call.
LEAKY_ENV_PREFIXES = ("FRESHRSS_", "RESEND_", "NEWSLETTER_", "LITELLM_")


@pytest.fixture(autouse=True)
def _scrubbed_env(monkeypatch):
    """Remove real credentials and any inherited PASSBAND_* override."""
    for key in list(os.environ):
        if key.startswith(LEAKY_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    # Every PASSBAND_* var is an override of something a test asserts
    # on: a developer with PASSBAND_CONFIG_DIR exported reads the wrong config
    # directory, one with PASSBAND_BBOX or PASSBAND_FORECAST_POINT exported
    # sees env win where a test expects the YAML value, and either spelling of
    # DB_PATH/OUT_DIR moves the state paths out from under test_env_names.
    # Tests opt in explicitly via the config_dir fixture or monkeypatch.setenv.
    for key in list(os.environ):
        if key.startswith("PASSBAND_"):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture(autouse=True)
def _clear_config_cache(_scrubbed_env):
    """Clear the config cache before and after each test.

    Depends on _scrubbed_env so the ordering is explicit: env is scrubbed
    first, then anything cached under the old env is discarded.
    """
    config.cache_clear()
    yield
    config.cache_clear()


class ConfigDir:
    """A throwaway ``config/`` directory that ``config()`` will read from."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, name: str, content) -> Path:
        """Write one YAML file. ``content`` is raw YAML text or a dict."""
        target = self.path / name
        if not isinstance(content, str):
            content = yaml.safe_dump(content, allow_unicode=True, sort_keys=False)
        target.write_text(content, encoding="utf-8")
        config.cache_clear()
        return target

    def remove(self, name: str) -> None:
        """Delete a file if present, so degradation paths can be exercised."""
        (self.path / name).unlink(missing_ok=True)
        config.cache_clear()


@pytest.fixture
def config_dir(tmp_path, monkeypatch) -> ConfigDir:
    """Point PASSBAND_CONFIG_DIR at an empty tmp config directory.

    monkeypatch unwinds the env var at teardown; tmp_path is per-test.
    """
    path = tmp_path / "config"
    path.mkdir()
    monkeypatch.setenv("PASSBAND_CONFIG_DIR", str(path))
    config.cache_clear()
    return ConfigDir(path)
