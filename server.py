#!/usr/bin/env python3
"""Read-only MCP server over the Passband news store.

Exposes the same continuously-gathered + clustered data pool that feeds the
newsletter as agent tools: list_sections, latest, search, clusters. It never
writes — the hourly `gather` owns the DB; this opens read-only WAL
connections, so any number of agents can query concurrently while gather runs.

Run: `python server.py` (streamable-HTTP on PASSBAND_MCP_HOST:PASSBAND_MCP_PORT).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from passband import store
from passband.cluster import cluster_items
from passband.config import (
    MCP_SERVER_NAME,
    config,
    mcp_host,
    mcp_pool_cap,
    mcp_port,
)

mcp = FastMCP(MCP_SERVER_NAME, host=mcp_host(), port=mcp_port())

# Cap on how many items a single tool call pulls into memory before ranking.
_POOL_CAP = mcp_pool_cap()


def _section_titles() -> dict:
    return {s["id"]: s.get("title", s["id"]) for s in config()["sections"]}


def _fmt(item) -> dict:
    return {
        "title": item.title,
        "url": item.url,
        "source": item.source_title,
        "section": item.section,
        "published": (
            datetime.fromtimestamp(item.published, timezone.utc).isoformat()
            if item.published else None
        ),
        "summary": (item.summary or "")[:400],
    }


@mcp.tool()
def list_sections() -> dict:
    """List the news sections (categories) and how many stored items each holds.
    Use the returned `id` values to filter the other tools."""
    titles = _section_titles()
    with store.connect_read() as conn:
        c = store.counts(conn)
    sections = [
        {"id": sid, "title": titles.get(sid, sid), "items": n}
        for sid, n in sorted(c["by_section"].items(), key=lambda kv: -kv[1])
    ]
    return {"total_items": c["total"], "sections": sections}


@mcp.tool()
def latest(section: str = "", hours: int = 24, limit: int = 20) -> list:
    """Most recent stored news items, newest first. Optionally filter to one
    section id (see list_sections). `hours` is the lookback window."""
    since = int(time.time()) - max(1, hours) * 3600
    with store.connect_read() as conn:
        items = store.recent_items(conn, since, section=section or None,
                                   limit=max(1, min(limit, 100)))
    return [_fmt(i) for i in items]


@mcp.tool()
def search(query: str, section: str = "", hours: int = 168, limit: int = 15) -> list:
    """Relevance search over stored news (TF-IDF cosine). Use this for
    'what's the latest on X' questions. `hours` bounds how far back to look;
    optionally restrict to one section id."""
    since = int(time.time()) - max(1, hours) * 3600
    with store.connect_read() as conn:
        pool = store.recent_items(conn, since, section=section or None, limit=_POOL_CAP)
    limit = max(1, min(limit, 50))
    if not query.strip() or not pool:
        return [_fmt(i) for i in pool[:limit]]

    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    corpus = [i.text for i in pool]
    vec = TfidfVectorizer(stop_words="english", sublinear_tf=True)
    try:
        m = vec.fit_transform(corpus + [query])
    except ValueError:
        return [_fmt(i) for i in pool[:limit]]
    sims = cosine_similarity(m[-1], m[:-1]).ravel()
    ranked = sorted(zip(pool, sims), key=lambda t: t[1], reverse=True)

    out = []
    for item, score in ranked[:limit]:
        if score <= 0:
            break
        d = _fmt(item)
        d["score"] = round(float(score), 3)
        out.append(d)
    return out


@mcp.tool()
def clusters(section: str = "", hours: int = 48, max_clusters: int = 10) -> list:
    """Group recent news into story clusters (the same story across outlets),
    most-corroborated and newest first. Returns each cluster's lead story,
    sources, and member headlines. Optionally restrict to one section id."""
    since = int(time.time()) - max(1, hours) * 3600
    with store.connect_read() as conn:
        items = store.recent_items(conn, since, section=section or None, limit=_POOL_CAP)
    out = []
    for c in cluster_items(items)[:max(1, min(max_clusters, 30))]:
        rep = c.representative
        out.append({
            "lead": rep.title,
            "url": rep.url,
            "summary": (rep.summary or "")[:400],
            "section": rep.section,
            "size": len(c.items),
            "sources": c.sources,
            "headlines": [i.title for i in c.items][:8],
        })
    return out


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
