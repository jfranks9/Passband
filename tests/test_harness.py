"""Unit 0 — the harness itself.

passband/config.py runs load_dotenv(ROOT / ".env") at *import* time, i.e. during
collection, before any fixture. These tests pin the scrub that undoes it.
"""
from __future__ import annotations

import os

from passband.config import config

# Stated independently of conftest on purpose: this test asserts the contract,
# it does not re-use the implementation's constant.
LEAKY_PREFIXES = ("FRESHRSS_", "RESEND_", "NEWSLETTER_", "LITELLM_")


def test_credential_env_vars_are_scrubbed():
    """No FRESHRSS_/RESEND_/NEWSLETTER_/LITELLM_ value survives into a test."""
    leaked = [k for k in os.environ if k.startswith(LEAKY_PREFIXES)]
    assert leaked == []


def test_config_env_block_holds_no_real_credentials():
    """config()["env"] falls back to its harmless defaults under the scrub."""
    env = config()["env"]
    assert env["freshrss_url"] == ""
    assert env["freshrss_user"] == ""
    assert env["freshrss_password"] == ""
    assert env["resend_api_key"] == ""
    assert env["newsletter_from"] == ""
    assert env["newsletter_to"] == ""
    # Defaults only — nothing that points at a real LiteLLM instance.
    assert env["litellm_base_url"] == "http://localhost:4000"
    assert env["litellm_api_key"] == "sk-local"
