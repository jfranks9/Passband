"""Dedupe + TF-IDF story clustering. No LLM here — this is what bounds cost.

Cheap tier: content_hash dedupe already happens at the DB layer.
This tier: group near-identical/related items (same story across outlets) so the
curator summarizes ~N clusters instead of every raw item.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Item

# Cosine similarity at/above which two items are considered the same story.
# Tuned for char n-gram TF-IDF (see cluster_items): catches morphological
# variants like "quake"/"earthquake" that word-level TF-IDF misses. True
# synonyms ("advisory"/"warning") still need embeddings — see roadmap.
SIM_THRESHOLD = 0.30


@dataclass
class Cluster:
    items: list[Item] = field(default_factory=list)

    @property
    def representative(self) -> Item:
        # Prefer the longest summary as the representative (most informative).
        return max(self.items, key=lambda i: len(i.summary or ""))

    @property
    def sources(self) -> list[str]:
        seen, out = set(), []
        for i in self.items:
            s = i.source_title or i.url
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def snippet(self, max_chars: int) -> str:
        rep = self.representative
        text = rep.summary or rep.title
        return text[:max_chars]


def _union_find(n: int):
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    return find, union


def cluster_items(items: list[Item], threshold: float = SIM_THRESHOLD) -> list[Cluster]:
    items = [i for i in items if i.text.strip()]
    if not items:
        return []
    if len(items) == 1:
        return [Cluster(items=items)]

    # Lazy import keeps sklearn off the import path for non-clustering modes.
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    # Char n-grams are robust to morphological variants and short headlines.
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True)
    try:
        matrix = vec.fit_transform([i.text for i in items])
    except ValueError:
        # e.g. all stop words; fall back to one-item-per-cluster
        return [Cluster(items=[i]) for i in items]

    sim = cosine_similarity(matrix)
    find, union = _union_find(len(items))
    for a in range(len(items)):
        for b in range(a + 1, len(items)):
            if sim[a, b] >= threshold:
                union(a, b)

    groups: dict[int, Cluster] = {}
    for idx, item in enumerate(items):
        root = find(idx)
        groups.setdefault(root, Cluster()).items.append(item)

    clusters = list(groups.values())
    # Bigger clusters (more corroboration) and newer stories first.
    clusters.sort(
        key=lambda c: (len(c.items), max(i.published for i in c.items)),
        reverse=True,
    )
    return clusters
