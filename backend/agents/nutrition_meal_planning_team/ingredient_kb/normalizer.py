"""Deterministic string normalization for ingredient matching.

SPEC-005 §4.7. Applied in a fixed order to both sides of alias
lookup (index and query) so callers can write aliases in natural
form.

No ML, no LLM. Lowercasing, NFKC, diacritic strip, simple
pluralization with an overrides table, conservative punctuation
handling.
"""

from __future__ import annotations

import re
import unicodedata

# Plural → singular overrides for irregular English forms that the
# trailing-s heuristic gets wrong. Checked BEFORE the heuristic.
_IRREGULAR_PLURALS: dict[str, str] = {
    "tomatoes": "tomato",
    "potatoes": "potato",
    "leaves": "leaf",
    "knives": "knife",
    "loaves": "loaf",
    "wolves": "wolf",
    "shelves": "shelf",
    "lives": "life",
    "calves": "calf",
    "halves": "half",
    "chickpeas": "chickpea",
    "berries": "berry",
    "cherries": "cherry",
    "cranberries": "cranberry",
    "blueberries": "blueberry",
    "strawberries": "strawberry",
    "raspberries": "raspberry",
}

# Filler tokens dropped when they appear adjacent to a unit keyword.
# We do NOT strip "of" unconditionally (it's part of "juice of 1 lemon").
_UNIT_ADJACENT_STOPWORDS = {"of", "the"}

_PUNCT_KEEP = {","}  # keep interior commas (the parser uses them)


def strip_diacritics(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _depluralize_word(word: str) -> str:
    """Conservative single-word plural → singular."""
    if word in _IRREGULAR_PLURALS:
        return _IRREGULAR_PLURALS[word]
    # Trailing "ies" → "y" (berries → berry) — but only if the word
    # is long enough that "ies" is plural, not a stem.
    if len(word) > 4 and word.endswith("ies") and not word.endswith("eies"):
        return word[:-3] + "y"
    # Trailing "es" → "" only after common es-plural endings
    # (s, x, z, ch, sh). Avoids stripping "lemonies" → "lemoni".
    if len(word) > 3 and word.endswith("es"):
        stem = word[:-2]
        if stem.endswith(("s", "x", "z", "ch", "sh")):
            return stem
    # Plain trailing "s" plural — strip only for words >3 chars and
    # when not ending in "ss" (kisses, glasses).
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss") and not word.endswith("us"):
        return word[:-1]
    return word


def normalize(s: str) -> str:
    """Canonical-form normalizer applied to both sides of alias lookup.

    Steps (fixed order per SPEC-005 §4.7):
    1. NFKC + strip diacritics.
    2. Lowercase.
    3. Strip punctuation except interior commas.
    4. Tokenize on whitespace.
    5. Depluralize each token (irregular overrides, then heuristic).
    6. Drop unit-adjacent stopwords ("of", "the") when they appear
       between numbers/units and content.
    7. Collapse whitespace.
    """
    if not s:
        return ""
    # 1
    s = strip_diacritics(s)
    # 2
    s = s.lower()
    # 3 — strip punctuation except , and keep interior dashes/apostrophes
    # (common in ingredient names: "extra-virgin", "grandma's").
    cleaned = []
    for ch in s:
        if ch.isalnum() or ch.isspace() or ch in _PUNCT_KEEP or ch in "-'":
            cleaned.append(ch)
        else:
            cleaned.append(" ")
    s = "".join(cleaned)
    # 4
    tokens = [t for t in re.split(r"\s+", s) if t]
    # 5
    tokens = [_depluralize_word(t) for t in tokens]
    # 6 — conservative: drop stopwords only when they appear between
    # non-stopword tokens (protects "juice of 1 lemon" by removing
    # "of" but keeping "juice" and "lemon").
    if len(tokens) > 2:
        filtered: list[str] = []
        for i, tok in enumerate(tokens):
            if tok in _UNIT_ADJACENT_STOPWORDS and 0 < i < len(tokens) - 1:
                continue
            filtered.append(tok)
        tokens = filtered
    # 7
    return " ".join(tokens)
