"""Section cadence: when does a section render?

Three cadences:
  daily         — every run.
  weekly        — on `weekday` (0=Mon..6=Sun) or any day in `weekdays: [..]`.
  breakthrough  — never scheduled; renders only when the section's buckets are
                  running unusually hot versus their own recent history.

Breakthrough logic (deterministic, self-adjusting, spike-robust):
  * baseline = median of the trailing `baseline_days` (default 28) 24h-window
    item counts, EXCLUDING the current 24h window. A median tracks slow shifts
    in a bucket's normal volume but barely moves for a single wild week, which
    is exactly the "auto-adjusting but not twitchy" behavior wanted.
  * due when  last_24h_count >= max(min_items, multiplier * baseline).
  * cold start: fewer than `min_history_days` of stored history -> never due
    (prevents alert spam while baselines are still forming).

Per-section overrides live under a `breakthrough:` block in sections.yaml.
"""
from __future__ import annotations

import statistics
import time
from datetime import datetime

from . import store

DEFAULTS = {
    "baseline_days": 28,
    "multiplier": 2.5,
    "min_items": 8,
    "min_history_days": 7,
}


def _buckets(section: dict) -> list[str]:
    """Buckets feeding a section; composite sections declare `sources`."""
    return section.get("sources") or [section["id"]]


def breakthrough_status(section: dict, conn) -> tuple[bool, str | None]:
    override = section.get("breakthrough")
    p = {**DEFAULTS, **(override if isinstance(override, dict) else {})}
    now = int(time.time())
    buckets = _buckets(section)

    if store.history_days(conn, buckets, now) < p["min_history_days"]:
        return False, None

    counts = store.window_counts(conn, buckets, now, p["baseline_days"])
    current, history = counts[0], counts[1:]
    baseline = statistics.median(history) if history else 0.0
    threshold = max(p["min_items"], p["multiplier"] * baseline)

    if current >= threshold:
        ratio = (current / baseline) if baseline else float(current)
        note = f"{current} items in 24h — {ratio:.1f}× the {p['baseline_days']}-day baseline"
        return True, note
    return False, None


def section_due(section: dict, today: datetime, conn) -> tuple[bool, str | None, str | None]:
    """(due, note, trigger).

    trigger is 'scheduled' or 'breakthrough' — main.py uses it to pick the
    lookback: a scheduled deep-dive covers its full lookback_hours, while a
    breakthrough render only covers the spike (breakthrough.lookback_hours,
    default 36) so a volume anomaly doesn't drag in a week of backlog.

    The breakthrough check is an OVERLAY: any section may set
    `breakthrough: true` (or a params dict) and it will force-render on a
    volume anomaly even on days its schedule wouldn't fire. cadence:
    'breakthrough' means overlay-only (never scheduled). cadence: 'rotate'
    is scheduled by the rotation selector in main.py, not here — this
    function only contributes the overlay for rotate sections.
    """
    cadence = section.get("cadence", "daily")
    if cadence == "daily":
        return True, None, "scheduled"
    if cadence == "weekly":
        days = section.get("weekdays") or [section.get("weekday", 0)]
        if today.weekday() in days:
            return True, None, "scheduled"
    if cadence == "breakthrough" or section.get("breakthrough"):
        due, note = breakthrough_status(section, conn)
        if due:
            return True, note, "breakthrough"
    return False, None, None


def rotation_pick(pool: list[dict], cursor: int, slots: int,
                  try_render) -> tuple[list[dict], int]:
    """Round-robin selection of up to `slots` deep-dive sections.

    Walks the pool once starting at `cursor`, calling try_render(section) on
    each candidate; a False return (nothing unsent to show) moves on to the
    next topic — skip-if-empty is what makes '1 or 2 per day' emerge
    naturally. Returns (selected, new_cursor); new_cursor sits just past the
    last examined slot so skipped-empty topics rotate to the back rather than
    jamming the queue.
    """
    if not pool or slots <= 0:
        return [], cursor
    selected = []
    advanced = 0
    for k in range(len(pool)):
        s = pool[(cursor + k) % len(pool)]
        advanced = k + 1
        if try_render(s):
            selected.append(s)
            if len(selected) >= slots:
                break
    if not selected:
        return [], cursor
    return selected, (cursor + advanced) % len(pool)
