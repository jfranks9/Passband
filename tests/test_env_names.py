"""Environment variable handling.

Everything location-specific or secret resolves through ``PASSBAND_*`` variables,
read via helper *functions* rather than module constants. A constant evaluated at
import cannot be tested at all — pytest imports the module during collection, long
before ``monkeypatch.setenv`` runs. Same seam as ``_config_dir()``.
"""
from __future__ import annotations

from pathlib import Path

from passband.config import (
    MCP_SERVER_NAME,
    ROOT,
    db_path,
    mcp_host,
    mcp_pool_cap,
    mcp_port,
    out_dir,
)


# ---------------------------------------------------------------------------
# State paths
# ---------------------------------------------------------------------------

def test_state_paths_from_env(monkeypatch):
    monkeypatch.setenv("PASSBAND_DB_PATH", "/srv/passband/passband.db")
    monkeypatch.setenv("PASSBAND_OUT_DIR", "/srv/passband/out")

    assert db_path() == Path("/srv/passband/passband.db")
    assert out_dir() == Path("/srv/passband/out")


def test_state_paths_default_when_unset(monkeypatch):
    """Pins the no-env default.

    A deployment that sets neither must still land on repo-relative paths rather
    than silently resolving to nothing.
    """
    monkeypatch.delenv("PASSBAND_DB_PATH", raising=False)
    monkeypatch.delenv("PASSBAND_OUT_DIR", raising=False)

    assert db_path() == ROOT / "store" / "passband.db"
    assert out_dir() == ROOT / "out"


def test_blank_value_counts_as_unset(monkeypatch):
    """An empty or whitespace-only value is not a value.

    Orchestrators routinely write empty strings for declared-but-unfilled
    variables. Treating one as set would resolve the store relative to the
    process working directory — a fresh, empty database on deploy, with nothing
    failing and no log line. The strip happens before the emptiness test for
    exactly this reason.
    """
    monkeypatch.delenv("PASSBAND_DB_PATH", raising=False)
    assert db_path() == ROOT / "store" / "passband.db"

    monkeypatch.setenv("PASSBAND_DB_PATH", "")
    assert db_path() == ROOT / "store" / "passband.db"

    monkeypatch.setenv("PASSBAND_DB_PATH", "   ")
    assert db_path() == ROOT / "store" / "passband.db"


# ---------------------------------------------------------------------------
# MCP server identity
#
# The name and the bind resolution live in config.py, not server.py, because
# server.py imports mcp.server.fastmcp at module scope: a test reaching for them
# there would need the whole MCP stack installed just to assert on a string.
# ---------------------------------------------------------------------------

MCP_ENV = ("PASSBAND_MCP_HOST", "PASSBAND_MCP_PORT", "PASSBAND_MCP_POOL_CAP")


def _clear_mcp_env(monkeypatch):
    for name in MCP_ENV:
        monkeypatch.delenv(name, raising=False)


def test_mcp_server_name():
    """The advertised name is the one agents will bind their config to."""
    assert MCP_SERVER_NAME == "passband-news"


def test_mcp_defaults_when_no_env(monkeypatch):
    """Bind defaults match what the shipped compose assumes."""
    _clear_mcp_env(monkeypatch)

    assert mcp_host() == "0.0.0.0"
    assert mcp_port() == 8000
    assert mcp_pool_cap() == 2000


def test_mcp_env_override(monkeypatch):
    _clear_mcp_env(monkeypatch)
    monkeypatch.setenv("PASSBAND_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("PASSBAND_MCP_PORT", "8020")
    monkeypatch.setenv("PASSBAND_MCP_POOL_CAP", "500")

    assert mcp_host() == "127.0.0.1"
    assert mcp_port() == 8020
    assert mcp_pool_cap() == 500


def test_mcp_malformed_port_falls_back_rather_than_crashing(monkeypatch):
    """A bad port must not raise at import — that would crash-loop the container.

    int() on a typo would propagate out of module scope in server.py and the
    service would restart forever with a bare ValueError instead of serving on
    the default.
    """
    _clear_mcp_env(monkeypatch)
    monkeypatch.setenv("PASSBAND_MCP_PORT", "not-a-port")
    assert mcp_port() == 8000

    monkeypatch.setenv("PASSBAND_MCP_POOL_CAP", "")
    assert mcp_pool_cap() == 2000
