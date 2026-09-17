"""FreshRSS collector via the Google Reader-compatible API.

Auth: POST .../accounts/ClientLogin with the FreshRSS *API password*
(Settings > Profile in FreshRSS), then send `Authorization: GoogleLogin auth=<token>`.
We only read (reading-list stream), so we don't need a write (T) token.
"""
from __future__ import annotations

import html
import re
from typing import Iterable

import requests

from ..config import config
from ..models import Item

GREADER = "/api/greader.php"
_LABEL = re.compile(r"user/[^/]+/label/(.+)$")


class FreshRSSError(RuntimeError):
    pass


def _login(base: str, user: str, password: str, session: requests.Session) -> str:
    resp = session.post(
        f"{base}{GREADER}/accounts/ClientLogin",
        data={"Email": user, "Passwd": password},
        timeout=30,
    )
    if resp.status_code != 200:
        raise FreshRSSError(f"ClientLogin failed ({resp.status_code}): {resp.text[:200]}")
    token = None
    for line in resp.text.splitlines():
        if line.startswith("Auth="):
            token = line[len("Auth="):].strip()
    if not token:
        raise FreshRSSError("ClientLogin returned no Auth token (check API password).")
    return token


def _normalize(name: str) -> str:
    """Fold a folder name to its match key.

    strip -> collapse internal whitespace -> unescape ``&amp;`` -> casefold.
    ``str.split()`` with no argument does the strip and the collapse in one
    pass, and also folds tabs/newlines into single spaces.
    """
    return html.unescape(" ".join(str(name).split())).casefold()


def _normalized_map(category_map: dict) -> dict:
    """Build the normalized lookup once, not once per item.

    ``setdefault`` means that when two keys collapse to the same normalized
    form, the first one in the map wins the *normalized* slot. Both keep their
    own exact entry, and exact is always tried first, so this only decides
    which of two colliding keys a third, inexact spelling resolves to.
    """
    normalized: dict = {}
    for key, section in category_map.items():
        normalized.setdefault(_normalize(key), section)
    return normalized


def _labels(categories: list[str]) -> list[str]:
    """The FreshRSS folder names on an item, in payload order.

    Non-label categories (``user/-/state/com.google/reading-list``) are dropped.
    """
    return [m.group(1) for m in (_LABEL.match(c) for c in categories) if m]


def _match_section(categories: list[str], category_map: dict,
                   normalized: dict | None = None) -> str | None:
    """The section id for these categories, or None if nothing matched.

    THE single matching function. ``_section_for`` and ``_parse``'s unmapped
    diagnostic both go through it; if they disagreed, the gather log would
    report tolerantly-matched items as unmapped.

    Exact wins over normalized across the *whole* category list: the exact
    pass runs to completion before the normalized pass starts, so an exact hit
    on the last label beats a normalized hit on the first.

    Returning None rather than a default is what lets the caller distinguish
    "matched, and the section happens to be the default" (``News - Global`` ->
    ``global``) from "nothing matched".
    """
    labels = _labels(categories)
    for label in labels:
        if label in category_map:
            return category_map[label]
    if normalized is None:
        normalized = _normalized_map(category_map)
    for label in labels:
        section = normalized.get(_normalize(label))
        if section is not None:
            return section
    return None


def _section_for(categories: list[str], category_map: dict, default: str,
                 normalized: dict | None = None) -> str:
    """Map FreshRSS folder labels on an item to a newsletter section id."""
    section = _match_section(categories, category_map, normalized)
    return default if section is None else section


def fetch_items(limit: int | None = None) -> list[Item]:
    cfg = config()
    env = cfg["env"]
    base = env["freshrss_url"].rstrip("/")
    if not (base and env["freshrss_user"] and env["freshrss_password"]):
        raise FreshRSSError(
            "FreshRSS not configured. Set FRESHRSS_URL / FRESHRSS_USER / "
            "FRESHRSS_API_PASSWORD in .env"
        )

    fr = cfg["sources"].get("freshrss", {})
    category_map = fr.get("category_map", {})
    default_section = fr.get("default_section", "global")
    n = limit or fr.get("fetch_limit", 400)

    session = requests.Session()
    token = _login(base, env["freshrss_user"], env["freshrss_password"], session)
    session.headers["Authorization"] = f"GoogleLogin auth={token}"

    resp = session.get(
        f"{base}{GREADER}/reader/api/0/stream/contents/"
        "user/-/state/com.google/reading-list",
        params={"output": "json", "n": n},
        timeout=60,
    )
    if resp.status_code != 200:
        raise FreshRSSError(f"stream fetch failed ({resp.status_code}): {resp.text[:200]}")

    unmapped: dict[str, int] = {}
    items = list(_parse(resp.json(), category_map, default_section, unmapped))
    if unmapped:
        # Every unmapped FreshRSS category silently dumps into
        # default_section — if another feed set (e.g. the personal RSS
        # collection) shares this FreshRSS instance, that pollutes the
        # newsletter. This makes the leak visible in the gather log.
        top = sorted(unmapped.items(), key=lambda kv: -kv[1])[:10]
        print(f"gather: {sum(unmapped.values())} items from unmapped categories "
              f"-> '{default_section}': "
              + ", ".join(f"{k} ({v})" for k, v in top))
    return items


def _parse(payload: dict, category_map: dict, default_section: str,
           unmapped: dict[str, int] | None = None) -> Iterable[Item]:
    # Built once per call, not once per item.
    normalized = _normalized_map(category_map)

    for it in payload.get("items", []):
        cats = it.get("categories", []) or []
        matched = _match_section(cats, category_map, normalized)
        section = default_section if matched is None else matched
        if unmapped is not None and matched is None:
            # `matched is None` means no label matched by *either* rule, so
            # this counts exactly what it claims to. Comparing `section ==
            # default_section` instead would miscount every correctly-mapped
            # `News - Global` item, since that key maps to `global`, which is
            # also the default.
            labels = _labels(cats)
            key = labels[0] if labels else "(no category)"
            unmapped[key] = unmapped.get(key, 0) + 1

        url = ""
        for alt in it.get("alternate", []) or it.get("canonical", []) or []:
            if alt.get("href"):
                url = alt["href"]
                break

        summary = (it.get("summary") or {}).get("content", "") or \
                  (it.get("content") or {}).get("content", "")

        published = int(it.get("published") or it.get("timestampUsec", 0) or 0)
        if published > 10_000_000_000:  # microseconds -> seconds
            published //= 1_000_000

        yield Item(
            title=it.get("title", "(untitled)"),
            url=url,
            summary=summary,
            section=section,
            source_title=(it.get("origin") or {}).get("title", ""),
            source_id=(it.get("origin") or {}).get("streamId", ""),
            published=published,
        )
