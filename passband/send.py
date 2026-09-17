"""Send the newsletter via Resend."""
from __future__ import annotations

import requests

from .config import config

RESEND_ENDPOINT = "https://api.resend.com/emails"


class SendError(RuntimeError):
    pass


def send_email(subject: str, html: str, dry_run: bool = False) -> dict:
    env = config()["env"]
    if dry_run:
        return {"dry_run": True, "to": env["newsletter_to"], "subject": subject}

    if not (env["resend_api_key"] and env["newsletter_from"] and env["newsletter_to"]):
        raise SendError(
            "Resend not configured. Set RESEND_API_KEY / NEWSLETTER_FROM / "
            "NEWSLETTER_TO in .env"
        )

    to = [a.strip() for a in env["newsletter_to"].split(",") if a.strip()]
    resp = requests.post(
        RESEND_ENDPOINT,
        headers={"Authorization": f"Bearer {env['resend_api_key']}"},
        json={"from": env["newsletter_from"], "to": to, "subject": subject, "html": html},
        timeout=30,
    )
    if resp.status_code >= 300:
        raise SendError(f"Resend failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()
