"""Canonical item schema + normalization helpers."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

# Tracking params stripped during URL canonicalization (dedupe across sources).
_TRACKING = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "cmpid",
}
_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")


def canonical_url(url: str) -> str:
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return url.strip()
    query = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in _TRACKING]
    netloc = p.netloc.lower()
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), netloc, path, "", urlencode(query), ""))


def clean_text(text: str) -> str:
    if not text:
        return ""
    return _WS.sub(" ", _TAG.sub(" ", text)).strip()


def content_hash(title: str, url: str) -> str:
    basis = (clean_text(title).lower() + "|" + canonical_url(url)).encode("utf-8")
    return hashlib.sha256(basis).hexdigest()[:32]


@dataclass
class Item:
    title: str
    url: str
    summary: str
    section: str
    source_title: str = ""
    source_id: str = ""
    published: int = 0  # epoch seconds
    content_hash: str = field(default="")

    def __post_init__(self):
        self.title = clean_text(self.title)
        self.summary = clean_text(self.summary)
        self.url = canonical_url(self.url)
        if not self.content_hash:
            self.content_hash = content_hash(self.title, self.url)

    @property
    def text(self) -> str:
        """Combined text used for clustering/snippets."""
        return f"{self.title}. {self.summary}".strip()
