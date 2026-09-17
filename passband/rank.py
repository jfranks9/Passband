"""Interest-weighted cluster ranking.

cluster_items() orders by (size, recency) — corroboration first. That is the
right default, but for steered sections (cyber) "big story" and "story I care
about" diverge: seven firehose outlets will always corroborate the generic
breach-of-the-week. This stage re-orders clusters by keyword interest BEFORE
the max_clusters cut, so relevance decides what survives the cap.

Scoring is deliberately dumb and inspectable:
  +1 per distinct boost term matched anywhere in the cluster's text
  -1 per distinct demote term matched
Ties fall back to the original (size, recency) order. A single-source item
matching two boost terms outranks a five-source generic cluster — intended.

Terms are case-insensitive substrings; multi-word terms match as phrases.
Everything lives in sections.yaml (`boost:` / `demote:` lists) — no code
change to retune.
"""
from __future__ import annotations

from .cluster import Cluster


def _cluster_text(cluster: Cluster) -> str:
    parts = []
    for item in cluster.items:
        parts.append(item.title)
        parts.append(item.summary[:400])
    return " ".join(parts).lower()


def interest_score(cluster: Cluster, boost: list[str], demote: list[str]) -> int:
    text = _cluster_text(cluster)
    score = sum(1 for term in boost if term.lower() in text)
    score -= sum(1 for term in demote if term.lower() in text)
    return score


def rank_clusters(
    clusters: list[Cluster],
    boost: list[str] | None = None,
    demote: list[str] | None = None,
) -> list[Cluster]:
    """Stable re-order by interest score; no-op when no terms configured."""
    if not boost and not demote:
        return clusters
    boost, demote = boost or [], demote or []
    # sorted() is stable: equal scores keep cluster_items()' size/recency order.
    return sorted(
        clusters,
        key=lambda c: interest_score(c, boost, demote),
        reverse=True,
    )
