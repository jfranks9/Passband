"""Alert layer: structured USGS/NWS collectors + keyword watchlist.

Design constraints:
  * Never break the newsletter run — every network call is wrapped; a dead
    API degrades to "no structured alerts today", printed, not raised.
  * Deterministic, no LLM. Alert bodies are the source's own text, trimmed.
  * Idempotent: structured events land in risk_events (PK = event id);
    rendering is deduped via sent_log under the reserved section id 'alerts'.
  * --dry-run skips the network entirely; the watchlist still runs over the
    local store so the whole layer is testable offline.
"""
from __future__ import annotations

import hashlib
import time

import requests

from .. import store
from ..config import config
from ..curate import Brief

ALERTS_SECTION = "alerts"
NWS_ENDPOINT = "https://api.weather.gov/alerts/active"
# api.weather.gov rejects requests without a UA identifying the client.
NWS_HEADERS = {"User-Agent": "passband (personal newsletter pipeline)",
               "Accept": "application/geo+json"}


def alert_hash(event_id: str) -> str:
    return hashlib.sha256(f"alert|{event_id}".encode("utf-8")).hexdigest()[:32]


def _in_bbox(lon, lat, bbox) -> bool:
    if not bbox or lon is None or lat is None:
        return False
    min_lon, min_lat, max_lon, max_lat = bbox
    return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat


# --------------------------------------------------------------------------
# Structured collectors
# --------------------------------------------------------------------------

def usgs_events(acfg: dict, geo: dict) -> list[dict]:
    url = config()["sources"].get("risk", {}).get("usgs_earthquakes")
    if not url:
        return []
    p = acfg.get("structured", {}).get("usgs", {})
    mag_global = p.get("min_magnitude_global", 6.0)
    mag_regional = p.get("min_magnitude_regional", 3.5)
    bbox = geo.get("bbox")

    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    out = []
    for feat in resp.json().get("features", []):
        props = feat.get("properties", {}) or {}
        mag = props.get("mag")
        if mag is None:
            continue
        coords = (feat.get("geometry") or {}).get("coordinates") or [None, None]
        lon, lat = coords[0], coords[1]
        regional = _in_bbox(lon, lat, bbox)
        if mag < (mag_regional if regional else mag_global):
            continue
        out.append({
            "id": feat.get("id") or props.get("code", ""),
            "category": "earthquake",
            "title": props.get("title", f"M{mag} earthquake"),
            "severity": float(mag),
            "lat": lat, "lon": lon,
            "occurred_at": int((props.get("time") or 0) / 1000),
            "source": "USGS",
            "url": props.get("url", ""),
            "body": (f"Magnitude {mag} — {props.get('place', 'location n/a')}."
                     + (" Within your regional bounding box." if regional else "")),
        })
    return out


def nws_events(acfg: dict) -> list[dict]:
    p = acfg.get("structured", {}).get("nws", {})
    area = p.get("area", "TX")
    sev_statewide = set(p.get("severities_statewide", ["Extreme"]))
    ev_statewide = set(p.get("events_statewide", []))
    ev_local = set(p.get("events_local", []))
    local_terms = [t.lower() for t in p.get("local_terms", [])]

    resp = requests.get(NWS_ENDPOINT, params={"area": area},
                        headers=NWS_HEADERS, timeout=30)
    resp.raise_for_status()
    out = []
    for feat in resp.json().get("features", []):
        props = feat.get("properties", {}) or {}
        event = props.get("event", "")
        severity = props.get("severity", "")
        area_desc = (props.get("areaDesc") or "").lower()

        statewide_hit = severity in sev_statewide or event in ev_statewide
        local_hit = event in ev_local and any(t in area_desc for t in local_terms)
        if not (statewide_hit or local_hit):
            continue

        headline = props.get("headline") or event
        desc = (props.get("description") or "")[:300]
        out.append({
            "id": props.get("id") or feat.get("id", ""),
            "category": "weather",
            "title": headline,
            "severity": 2.0 if severity == "Extreme" else 1.0,
            "lat": None, "lon": None,
            "occurred_at": None,
            "source": "NWS",
            "url": (props.get("@id") or feat.get("id") or ""),
            "body": f"{event} — {props.get('areaDesc', '')}. {desc}".strip(),
        })
    return out


# --------------------------------------------------------------------------
# Watchlist over already-gathered FreshRSS items
# --------------------------------------------------------------------------

def watchlist_hits(conn, acfg: dict) -> list[dict]:
    w = acfg.get("watchlist", {})
    rules = w.get("rules", [])
    if not rules:
        return []
    geo_terms = [t.lower() for t in w.get("geo_terms", [])]
    lookback = w.get("lookback_hours", 30)
    since = int(time.time()) - lookback * 3600

    hits = []
    seen_hashes = set()
    rows = conn.execute(
        "SELECT * FROM items WHERE published >= ?", (since,)
    ).fetchall()
    for r in rows:
        text = f"{r['title']} {r['summary'] or ''}".lower()
        for rule in rules:
            if not any(term.lower() in text for term in rule.get("any", [])):
                continue
            if rule.get("near") and not any(g in text for g in geo_terms):
                continue
            if r["content_hash"] in seen_hashes:
                break
            seen_hashes.add(r["content_hash"])
            hits.append({
                "id": r["content_hash"],
                "content_hash": r["content_hash"],
                "bucket": r["section"],
                "category": f"watchlist:{rule.get('name', '?')}",
                "title": r["title"],
                "severity": 1.0,
                "source": r["source_title"] or "",
                "url": r["url"],
                "body": (r["summary"] or r["title"])[:300],
            })
            break
    return hits


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def build_alerts(conn, dry_run: bool = False) -> tuple[list[Brief], list[tuple[str, str]]]:
    """Returns (briefs, sent_pairs). sent_pairs are (content_hash, section)
    rows for main.py to mark after a real send. Watchlist items are marked
    under BOTH 'alerts' and their home bucket so they don't reappear in a
    scheduled section the next day."""
    cfg = config()
    acfg = cfg.get("alerts", {})
    if not acfg.get("enabled", True):
        return [], []

    events: list[dict] = []
    if not dry_run:
        for name, fn, args in (("usgs", usgs_events, (acfg, cfg.get("geo", {}))),
                               ("nws", nws_events, (acfg,))):
            try:
                events.extend(fn(*args))
            except Exception as exc:  # degrade, never break the send
                print(f"alerts: {name} collector failed, skipping ({exc})")
        if events:
            store.upsert_risk_events(conn, [
                {k: e.get(k) for k in ("id", "category", "title", "severity",
                                       "lat", "lon", "occurred_at", "source", "url")}
                | {"raw": None}
                for e in events
            ])

    events.extend(watchlist_hits(conn, acfg))

    briefs, sent_pairs = [], []
    events.sort(key=lambda e: e.get("severity") or 0, reverse=True)
    for e in events:
        h = e.get("content_hash") or alert_hash(e["id"])
        if store.already_sent(conn, h, ALERTS_SECTION):
            continue
        briefs.append(Brief(
            title=e["title"],
            body=e.get("body") or "",
            url=e.get("url") or "",
            sources=[e.get("source")] if e.get("source") else [],
        ))
        sent_pairs.append((h, ALERTS_SECTION))
        if e.get("bucket"):
            sent_pairs.append((e["content_hash"], e["bucket"]))
        if len(briefs) >= acfg.get("max_items", 6):
            break
    return briefs, sent_pairs
