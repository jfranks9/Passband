"""Tolerant FreshRSS category matching (Unit 4).

``_section_for`` maps a FreshRSS folder label to a newsletter section id. It
tries an EXACT match first, then a normalized one. Normalization order:
strip -> collapse internal whitespace -> unescape ``&amp;`` -> casefold.
Exact always wins over normalized, across the whole category list.

The subtle requirement is the unmapped diagnostic. ``_parse`` counts items that
fell through to ``default_section`` so the gather log can show which FreshRSS
folders are unmapped. If that count keeps using exact matching while
``_section_for`` normalizes, every tolerantly-matched item is logged as
unmapped and the diagnostic lies. Both paths must share one matcher.

No network: ``_parse`` takes a plain payload dict, which is the seam. Nothing
here imports ``requests`` behaviour or touches ``fetch_items``.
"""
from __future__ import annotations

from passband.collectors.freshrss import _normalize, _parse, _section_for


def _cat(label: str) -> str:
    """Wrap a folder name in the Google Reader stream id FreshRSS emits."""
    return f"user/-/label/{label}"


# A non-label category every real FreshRSS item also carries. `_LABEL` must
# skip it; if it ever matched, every test below would trivially pass.
READING_LIST = "user/-/state/com.google/reading-list"


def _payload(*category_lists: list[str]) -> dict:
    """Minimal stream payload: one item per list of raw category strings."""
    return {
        "items": [
            {
                "title": f"item {i}",
                "categories": list(cats),
                "alternate": [{"href": f"https://example.invalid/{i}"}],
                "summary": {"content": "body"},
                "published": 1750000000,
                "origin": {"title": "Example", "streamId": "feed/https://example.invalid"},
            }
            for i, cats in enumerate(category_lists)
        ]
    }


CMAP = {
    "News - Texas": "texas",
    "Risk - Hazard & Disaster": "hazard",
    "News - Global": "global",
}
DEFAULT = "global"


def test_exact_label_match_unchanged():
    """The pre-existing exact path still works, and non-label cats are skipped."""
    assert _section_for([_cat("News - Texas")], CMAP, DEFAULT) == "texas"
    assert _section_for([_cat("Risk - Hazard & Disaster")], CMAP, DEFAULT) == "hazard"
    # State categories are not labels and must not be considered.
    assert _section_for([READING_LIST, _cat("News - Texas")], CMAP, DEFAULT) == "texas"
    assert _section_for([READING_LIST], CMAP, DEFAULT) == DEFAULT
    # First exact hit in payload order wins.
    assert _section_for(
        [_cat("News - Texas"), _cat("Risk - Hazard & Disaster")], CMAP, DEFAULT
    ) == "texas"


def test_case_insensitive_match():
    assert _section_for([_cat("news - texas")], CMAP, DEFAULT) == "texas"
    assert _section_for([_cat("NEWS - TEXAS")], CMAP, DEFAULT) == "texas"
    assert _section_for([_cat("News - TeXaS")], CMAP, DEFAULT) == "texas"


def test_trailing_whitespace_match():
    assert _section_for([_cat("News - Texas ")], CMAP, DEFAULT) == "texas"
    assert _section_for([_cat("  News - Texas")], CMAP, DEFAULT) == "texas"
    assert _section_for([_cat("\tNews - Texas  ")], CMAP, DEFAULT) == "texas"


def test_ampersand_entity_match():
    """FreshRSS usually decodes &amp;, but an OPML import may not have."""
    assert _section_for([_cat("Risk - Hazard &amp; Disaster")], CMAP, DEFAULT) == "hazard"
    assert _section_for([_cat("risk - hazard &amp; disaster")], CMAP, DEFAULT) == "hazard"
    # And the already-decoded form keeps working.
    assert _section_for([_cat("Risk - Hazard & Disaster")], CMAP, DEFAULT) == "hazard"


def test_collapsed_internal_whitespace_match():
    assert _section_for([_cat("News  -  Texas")], CMAP, DEFAULT) == "texas"
    assert _section_for([_cat("News\t-\tTexas")], CMAP, DEFAULT) == "texas"
    assert _section_for([_cat("Risk -  Hazard  &  Disaster")], CMAP, DEFAULT) == "hazard"


def test_unmapped_counter_uses_same_normalization():
    """The diagnostic must not report tolerantly-matched items as unmapped.

    Three cases in one, because they are the three ways the counter can lie:
      1. a normalized match must NOT be counted;
      2. an exact match whose section IS the default (``News - Global`` ->
         ``global``) must NOT be counted -- this is the case a naive refactor
         that drops the second check regresses;
      3. a genuinely unknown folder MUST still be counted.
    """
    payload = _payload(
        [_cat("news - texas ")],                 # normalized match -> texas
        [_cat("Risk - Hazard &amp; Disaster")],  # normalized match -> hazard
        [_cat("News - Global")],                  # exact match, section == default
        [_cat("Hobby - Ham Radio")],              # genuinely unmapped
        [READING_LIST],                           # no label at all
    )
    unmapped: dict[str, int] = {}
    items = list(_parse(payload, CMAP, DEFAULT, unmapped))  # generator: consume it

    assert [i.section for i in items] == ["texas", "hazard", "global", DEFAULT, DEFAULT]
    assert unmapped == {"Hobby - Ham Radio": 1, "(no category)": 1}


def test_ambiguous_normalized_collision_prefers_exact():
    """Exact beats normalized -- including when the exact hit comes later."""
    cmap = {
        "News - Texas": "texas_titlecase",
        "news - texas": "texas_lowercase",
    }
    # Each spelling resolves to its own exact entry.
    assert _section_for([_cat("News - Texas")], cmap, DEFAULT) == "texas_titlecase"
    assert _section_for([_cat("news - texas")], cmap, DEFAULT) == "texas_lowercase"

    # An exact hit later in the category list must beat a normalized hit
    # earlier in it: the exact pass runs over the whole list first.
    cats = [_cat("NEWS - TEXAS"), _cat("news - texas")]
    assert _section_for(cats, cmap, DEFAULT) == "texas_lowercase"

    # Sanity: the normalization these two keys collide under is real.
    assert _normalize("News - Texas") == _normalize("news - texas")


def test_unmapped_item_falls_to_default_section():
    """An unknown folder still lands in default_section, counted, not dropped."""
    payload = _payload(
        [_cat("Some Folder Nobody Mapped")],
        [_cat("Some Folder Nobody Mapped")],
        [],
    )
    unmapped: dict[str, int] = {}
    items = list(_parse(payload, CMAP, DEFAULT, unmapped))

    assert len(items) == 3
    assert all(i.section == DEFAULT for i in items)
    assert unmapped == {"Some Folder Nobody Mapped": 2, "(no category)": 1}

    # And the counter stays optional -- passing no dict must not raise.
    assert len(list(_parse(payload, CMAP, DEFAULT))) == 3
