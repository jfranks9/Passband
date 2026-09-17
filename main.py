#!/usr/bin/env python3
"""Passband — news + risk pipeline entrypoint.

Usage:
    python main.py gather                  # pull FreshRSS -> normalize -> SQLite
    python main.py gather --mock           # seed sample data (no creds needed)
    python main.py newsletter --dry-run    # build HTML, skip LLM network + send
    python main.py newsletter              # full: cluster -> LLM -> render -> Resend
    python main.py dashboard               # (structured risk feeds — later phase)

Designed for cron: one daily run. Each section's cadence decides what renders.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from passband import store
from passband.cadence import rotation_pick, section_due
from passband.cluster import cluster_items
from passband.collectors.alerts import build_alerts
from passband.collectors.forecast import forecast_line
from passband.config import config, out_dir
from passband.curate import curate_section
from passband.rank import rank_clusters
from passband.render import format_subject, newsletter_brand, render_newsletter
from passband.send import send_email


def cmd_gather(args) -> int:
    if args.mock:
        fixture = Path(__file__).parent / "tests" / "fixtures" / "sample_items.json"
        from passband.models import Item
        raw = json.loads(fixture.read_text(encoding="utf-8"))
        now = int(time.time())
        items = [
            Item(
                title=r["title"], url=r["url"], summary=r.get("summary", ""),
                section=r["section"], source_title=r.get("source_title", ""),
                published=now - r.get("age_hours", 1) * 3600,
            )
            for r in raw
        ]
    else:
        from passband.collectors.freshrss import fetch_items
        items = fetch_items(limit=args.limit)

    with store.connect() as conn:
        added = store.upsert_items(conn, items)
        c = store.counts(conn)
    print(f"gather: fetched {len(items)}, new {added}, total in store {c['total']}")
    for sec, n in sorted(c["by_section"].items()):
        print(f"  {sec}: {n}")
    return 0


def cmd_newsletter(args) -> int:
    cfg = config()
    today = datetime.now()
    rendered_sections = []
    included_hashes: list[tuple[str, str]] = []

    now = int(time.time())

    with store.connect() as conn:
        # Alert layer first: structured USGS/NWS + watchlist. Renders on top,
        # independent of every section's cadence. Skipped when a single
        # section is being forced (debugging).
        # used_hashes is the in-run dedupe ledger: buckets can feed several
        # sections the same day (Top News + a news-tier breakthrough/deep-dive,
        # tech + a hobby deep-dive), and sent_log only updates after the send.
        used_hashes: set[str] = set()
        if not args.section:
            alert_briefs, alert_sent_pairs = build_alerts(conn, dry_run=args.dry_run)
            if alert_briefs:
                rendered_sections.append(
                    {"title": "Alerts", "briefs": alert_briefs, "alert": True}
                )
                included_hashes.extend(alert_sent_pairs)
                used_hashes |= {h for h, _ in alert_sent_pairs}
                print(f"alerts: {len(alert_briefs)} firing")

        def render_one(section: dict, trigger: str, note: str | None) -> bool:
            """Cluster -> rank -> curate -> append one section. True if rendered."""
            # Breakthrough renders cover the spike, not the whole backlog.
            if trigger == "breakthrough":
                bt = section.get("breakthrough")
                bt = bt if isinstance(bt, dict) else {}
                lookback = bt.get("lookback_hours", 36)
            else:
                lookback = section.get("lookback_hours", 36)

            since = now - lookback * 3600
            buckets = section.get("sources") or [section["id"]]
            items = store.items_for_buckets(conn, buckets, since,
                                            exclude_sent=not args.dry_run)
            items = [i for i in items if i.content_hash not in used_hashes]
            if not items:
                return False

            clusters = cluster_items(items)
            clusters = rank_clusters(clusters,
                                     boost=section.get("boost"),
                                     demote=section.get("demote"))
            briefs = curate_section(
                clusters,
                llm_task=section.get("llm_task", "summarize"),
                max_clusters=section.get("max_clusters", 6),
                dry_run=args.dry_run,
            )
            if not briefs:
                return False

            title = section["title"]
            if trigger == "rotation":
                title = f"{title} — Deep Dive"
            rendered_sections.append(
                {"title": title, "briefs": briefs, "note": note}
            )
            for b in briefs:
                for c in clusters:
                    if c.representative.url == b.url:
                        # Mark by each item's OWN bucket so composite sections
                        # and deep-dives share one dedupe ledger.
                        included_hashes.extend(
                            (i.content_hash, i.section) for i in c.items
                        )
                        used_hashes.update(i.content_hash for i in c.items)
                        break
            return True

        # Pass 1 — scheduled + breakthrough sections in config order. Rotate
        # sections participate only via their breakthrough overlay here.
        pass1_rendered: set[str] = set()
        for section in cfg["sections"]:
            if args.section:
                if section["id"] == args.section:
                    render_one(section, "scheduled", None)
                continue
            due, note, trigger = section_due(section, today, conn)
            if due and render_one(section, trigger, note):
                pass1_rendered.add(section["id"])

        # Pass 2 — rotating deep-dive slots, selected AFTER the dailies have
        # consumed their share so a slot is never wasted on a topic the daily
        # composites just emptied. Round-robin; topics with nothing unsent are
        # skipped (that is how "1 or 2 per day" happens). Cursor persists in
        # SQLite and only advances on a real send, so dry-runs don't spin it.
        rot_cfg = cfg.get("rotation", {})
        rot_group = rot_cfg.get("group", "deep_dive")
        pool = [s for s in cfg["sections"] if s.get("cadence") == "rotate"]
        new_cursor = None
        if pool and not args.section:
            cursor = store.rotation_cursor(conn, rot_group)
            slots = rot_cfg.get("slots_per_day", 2)
            picked, new_cursor = rotation_pick(
                pool, cursor, slots,
                # Selection == successful render: render_one returns False on
                # empty topics (rotation_pick then tries the next candidate).
                # A topic that already breakthrough-rendered in pass 1 is
                # skipped — it had coverage today; its turn cycles around.
                lambda s: (s["id"] not in pass1_rendered
                           and render_one(s, "rotation", None)))
            if picked:
                print(f"rotation: {', '.join(s['id'] for s in picked)} "
                      f"(cursor {cursor} -> {new_cursor})")

        if not rendered_sections:
            print("newsletter: no due sections had content; nothing to send.")
            return 0

        header_note = None if args.dry_run else forecast_line()

        # Brand strings: config/sections.yaml -> newsletter:, all optional.
        title, footer, subject_format = newsletter_brand(cfg)
        html = render_newsletter(title, rendered_sections,
                                 header_note=header_note, footer=footer)

        out_root = out_dir()
        out_root.mkdir(parents=True, exist_ok=True)
        out_file = out_root / f"newsletter-{today:%Y%m%d}.html"
        out_file.write_text(html, encoding="utf-8")
        print(f"newsletter: rendered {len(rendered_sections)} sections -> {out_file}")

        subject = format_subject(subject_format, title, today)
        result = send_email(subject, html, dry_run=args.dry_run)
        print(f"send: {result}")

        if not args.dry_run:
            for h, sec in included_hashes:
                store.mark_sent(conn, [h], sec)
            print(f"sent_log: marked {len(included_hashes)} items")
            if new_cursor is not None:
                store.set_rotation_cursor(conn, rot_group, new_cursor,
                                          f"{today:%Y-%m-%d}")
    return 0


def cmd_dashboard(args) -> int:
    print("dashboard: structured risk feeds not yet wired (next phase). "
          "See config/sources.yaml -> risk.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Passband — news + risk pipeline")
    sub = p.add_subparsers(dest="mode", required=True)

    g = sub.add_parser("gather", help="pull + normalize + store")
    g.add_argument("--mock", action="store_true", help="seed sample data, no creds")
    g.add_argument("--limit", type=int, default=None)
    g.set_defaults(func=cmd_gather)

    n = sub.add_parser("newsletter", help="cluster -> curate -> render -> send")
    n.add_argument("--dry-run", action="store_true",
                   help="skip LLM network + email; don't mark items sent")
    n.add_argument("--section", default=None, help="render only this section id")
    n.set_defaults(func=cmd_newsletter)

    d = sub.add_parser("dashboard", help="structured risk feeds (later phase)")
    d.set_defaults(func=cmd_dashboard)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
