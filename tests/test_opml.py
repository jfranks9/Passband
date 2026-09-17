"""The shipped FreshRSS onboarding OPML must not drift from ``sources.yaml``.

``examples/feeds.opml`` is what a new user imports into FreshRSS. The
folder names it creates there are the exact strings ``category_map`` maps to
newsletter sections. If the two files disagree, the user gets a
plausible-looking newsletter in which everything has silently landed in
``default_section``.

These tests import ``_normalize`` from the collector rather than restating the
rules. Reimplementing normalization here would let the test agree with itself
while the collector did something else -- which is the exact failure mode the
tests exist to prevent.

The shipped file is read from disk; no network, and no ``config_dir`` fixture,
because the assertion is about the *committed* config and the *committed* OPML.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from passband.collectors.freshrss import _normalize
from passband.config import config

REPO_ROOT = Path(__file__).resolve().parent.parent
OPML_PATH = REPO_ROOT / "examples" / "feeds.opml"


def _body(root: ET.Element) -> ET.Element:
    body = root.find("body")
    assert body is not None, "OPML has no <body>"
    return body


def _name(outline: ET.Element) -> str:
    """An outline's display name.

    OPML requires ``text``; FreshRSS reads it. ``title`` is the optional
    mirror the shipped file also carries.
    """
    return outline.get("text") or outline.get("title") or ""


def _folders(root: ET.Element) -> list[ET.Element]:
    """Outlines with no ``xmlUrl`` -- i.e. FreshRSS categories, not feeds."""
    return [o for o in root.iter("outline") if not o.get("xmlUrl")]


def _feeds(root: ET.Element) -> list[ET.Element]:
    return [o for o in root.iter("outline") if o.get("xmlUrl")]


def _category_map() -> dict:
    return config()["sources"]["freshrss"]["category_map"]


def test_shipped_opml_parses():
    """Well-formed, non-empty, and flat two-level.

    The flatness assertion is load-bearing for the other two tests: they treat
    every folder as a top-level FreshRSS category. If someone nests folders,
    that assumption breaks here rather than silently elsewhere.
    """
    assert OPML_PATH.is_file(), f"missing shipped OPML: {OPML_PATH}"

    root = ET.parse(OPML_PATH).getroot()
    assert root.tag == "opml"

    body = _body(root)
    folders = _folders(root)
    feeds = _feeds(root)
    assert folders, "OPML defines no categories"
    assert feeds, "OPML defines no feeds"

    top_level = [o for o in body if o.tag == "outline" and not o.get("xmlUrl")]
    assert len(top_level) == len(folders), (
        "OPML is expected to be flat: every category a direct child of <body>"
    )

    for folder in folders:
        assert _name(folder).strip(), "a category outline has no text/title"
        title = folder.get("title")
        if title is not None:
            assert title == folder.get("text"), (
                f"text/title disagree on category {folder.get('text')!r}"
            )

    for feed in feeds:
        assert _name(feed).strip(), f"feed {feed.get('xmlUrl')} has no text/title"


def test_every_category_map_key_present_in_shipped_opml():
    """Every configured category maps onto a folder the import will create.

    The count is derived from the file, never hardcoded: the release plan says
    37 keys and the file has 36, so a magic number would have been wrong on
    day one.
    """
    category_map = _category_map()
    assert category_map, "sources.yaml has no freshrss.category_map"

    root = ET.parse(OPML_PATH).getroot()
    folder_names = {_normalize(_name(o)) for o in _folders(root)}

    missing = sorted(k for k in category_map if _normalize(k) not in folder_names)
    assert not missing, (
        f"{len(missing)} of {len(category_map)} category_map keys have no "
        f"folder in {OPML_PATH.name}: {missing}"
    )


def test_no_duplicate_category_names_in_opml():
    """Distinct folders on import.

    Compared after normalization, so a ``Tech - FOSS & Linux`` /
    ``Tech - FOSS &amp; Linux`` pair -- which FreshRSS would collapse into one
    category, quietly losing one folder's feeds -- fails here.
    """
    root = ET.parse(OPML_PATH).getroot()
    names = [_name(o) for o in _folders(root)]

    seen: dict[str, str] = {}
    duplicates: list[tuple[str, str]] = []
    for name in names:
        key = _normalize(name)
        if key in seen:
            duplicates.append((seen[key], name))
        else:
            seen[key] = name

    assert not duplicates, f"category names collide after normalization: {duplicates}"
