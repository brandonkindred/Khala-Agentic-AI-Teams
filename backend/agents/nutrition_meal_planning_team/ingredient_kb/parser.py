"""Deterministic ingredient-line parser.

SPEC-005 §4.5. Input: one free-text line. Output: a
``ParsedIngredient`` with resolved canonical_id, quantity, unit,
modifiers, confidence, and structured ``reasons`` when confidence
drops below 1.0.

No ML, no LLM. Grammar-light: qty/unit extraction with regex against
the known-unit set, head-noun + modifier split, alias lookup with
normalizer applied on both sides. Ambiguity is surfaced via
``reasons=('ambiguous',)`` rather than raising.
"""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Optional

from .catalog import get_alias_index, get_catalog
from .normalizer import normalize
from .types import ParsedIngredient, Unit
from .units import get_units, is_known_unit

# Modifiers commonly appearing as trailing words. Stripped from the
# head-noun when computing the canonical-id lookup but preserved in
# the output for downstream use (SPEC-007 guardrail context).
_TYPICAL_MODIFIERS = frozenset(
    {
        "diced",
        "chopped",
        "minced",
        "sliced",
        "grated",
        "shredded",
        "crushed",
        "whole",
        "halved",
        "quartered",
        "cubed",
        "peeled",
        "seeded",
        "deveined",
        "cooked",
        "raw",
        "boiled",
        "baked",
        "grilled",
        "fried",
        "steamed",
        "sauteed",
        "roasted",
        "boneless",
        "skinless",
        "drained",
        "rinsed",
        "fresh",
        "frozen",
        "dried",
        "softened",
        "melted",
        "room-temperature",
        "room",
        "temperature",
    }
)

# "No quantity specified" markers. Resolve canonical_id but leave
# qty/unit as None with a structured reason.
_TO_TASTE_PATTERNS = re.compile(
    r"\b(to taste|as needed|optional|a pinch of|pinch of|a dash of|dash of|a handful of|handful of|a few|some)\b",
    re.IGNORECASE,
)

# Quantity patterns we support:
#   "1", "1.5", "0.25", "1/2", "1 1/2", "1-2" (range → take first).
_QTY_PATTERN = re.compile(r"^(?:(\d+)\s+)?(\d+/\d+|\d+\.\d+|\d+)(?:\s*[-–]\s*\d+(?:\.\d+)?)?")

# "(14 oz)" package-size hint. When present we use the parenthesized
# amount, since the outer "1 can" is less informative than "14 oz".
_PAREN_SIZE_PATTERN = re.compile(r"\((\d+(?:\.\d+)?)\s*([a-z_]+)\)", re.IGNORECASE)

# Container words peeled after a paren-size hit. "1 can of chickpeas"
# leaves "chickpeas" once this strips "1 can of".
_CONTAINER_WORDS = frozenset(
    {
        "can",
        "cans",
        "jar",
        "jars",
        "bottle",
        "bottles",
        "package",
        "packages",
        "pkg",
        "box",
        "boxes",
        "bag",
        "bags",
        "carton",
        "cartons",
        "tin",
        "tins",
    }
)
_LEADING_CONTAINER_PATTERN = re.compile(
    r"^(?:\d+(?:\.\d+)?\s+)?(" + "|".join(sorted(_CONTAINER_WORDS)) + r")\b(?:\s+of)?\s*",
    re.IGNORECASE,
)

# "juice of 1 lemon" special case.
_JUICE_OF_PATTERN = re.compile(r"^juice of\s+(?:(\d+)\s+)?(lemon|lime|orange)s?$", re.IGNORECASE)


def _parse_qty_token(token: str) -> Optional[float]:
    """Convert a single qty token to float. Accepts '1/2', '1.5', '1'."""
    if "/" in token:
        try:
            return float(Fraction(token))
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(token)
    except ValueError:
        return None


def _extract_qty_and_unit(text: str) -> tuple[Optional[float], Optional[Unit], str]:
    """Peel qty + unit off the front of ``text``.

    Returns ``(qty_or_None, unit_or_None, remainder_text)``. The
    remainder is what gets fed to the alias lookup.
    """
    # Paren size wins over the outer number.
    m = _PAREN_SIZE_PATTERN.search(text)
    if m:
        qty_txt, unit_txt = m.group(1), m.group(2).lower()
        qty = _parse_qty_token(qty_txt)
        if is_known_unit(unit_txt):
            unit = get_units()[unit_txt]
            rest = text.replace(m.group(0), " ").strip()
            # Strip leading "N <container> of" so "1 (14 oz) can of
            # chickpeas" leaves just "chickpeas" for alias lookup.
            rest = _LEADING_CONTAINER_PATTERN.sub("", rest).strip()
            # Drop any stray leading "of" that the above left behind.
            if rest.lower().startswith("of "):
                rest = rest[3:].strip()
            return qty, unit, rest

    # Leading qty (+ possibly unit).
    match = _QTY_PATTERN.match(text.strip())
    if not match:
        return None, None, text
    whole_part, frac_part = match.group(1), match.group(2)
    consumed = match.group(0)
    qty_text = whole_part or ""
    if frac_part:
        qty_text = f"{qty_text} {frac_part}".strip()
    # Handle mixed fraction ("1 1/2") via Fraction.
    try:
        parts = qty_text.split()
        total = 0.0
        for p in parts:
            v = _parse_qty_token(p)
            if v is None:
                break
            total += v
        qty = total if parts else None
    except Exception:
        qty = None
    rest = text[len(consumed) :].strip()

    # Now look for a unit token.
    if not rest:
        return qty, None, ""
    tokens = rest.split()
    first = tokens[0].rstrip(",.").lower() if tokens else ""
    if is_known_unit(first):
        unit = get_units()[first]
        remainder = " ".join(tokens[1:])
        return qty, unit, remainder
    return qty, None, rest


def _split_modifiers(text: str) -> tuple[str, tuple[str, ...]]:
    """Peel trailing modifier words off the noun phrase.

    Handles both comma-separated ("chicken, diced") and
    space-separated ("diced chicken") forms. Returns the head noun
    and a tuple of modifiers.
    """
    # Comma form first.
    modifiers: list[str] = []
    if "," in text:
        head, _, tail = text.partition(",")
        for mod in tail.split(","):
            mod = mod.strip()
            if mod:
                modifiers.append(mod)
        text = head.strip()

    # Now strip leading/trailing modifier words from the head noun.
    tokens = text.split()
    # Leading modifiers: "diced chicken" → head "chicken", mod "diced"
    while tokens and tokens[0].lower() in _TYPICAL_MODIFIERS:
        modifiers.append(tokens.pop(0).lower())
    # Trailing modifiers: "chicken diced" → head "chicken", mod "diced"
    while tokens and tokens[-1].lower() in _TYPICAL_MODIFIERS:
        modifiers.append(tokens.pop(-1).lower())

    return " ".join(tokens), tuple(modifiers)


def parse_ingredient(raw: str) -> ParsedIngredient:
    """Parse one ingredient line → ``ParsedIngredient``. Never raises."""
    original = (raw or "").strip()
    if not original:
        return ParsedIngredient(
            raw=raw or "",
            qty=None,
            unit=None,
            name="",
            modifiers=(),
            canonical_id=None,
            confidence=0.0,
            reasons=("empty_input",),
        )

    reasons: list[str] = []

    # "juice of 1 lemon" handled specially — the canonical_id is
    # lemon, but qty is a count and the modifier set carries "juice".
    m = _JUICE_OF_PATTERN.match(original.strip())
    if m:
        qty_txt, fruit = m.group(1), m.group(2).lower()
        qty = float(qty_txt) if qty_txt else 1.0
        unit = get_units()["count"]
        canonical_id = fruit  # lemon / lime / orange
        if canonical_id not in get_catalog():
            canonical_id = None
            reasons.append("unknown_juice_of")
        return ParsedIngredient(
            raw=raw,
            qty=qty,
            unit=unit,
            name=fruit,
            modifiers=("juice",),
            canonical_id=canonical_id,
            confidence=0.9 if canonical_id else 0.0,
            reasons=tuple(reasons) if reasons else ("juice_of_count_unit",),
        )

    # To-taste / handful / pinch markers.
    to_taste_match = _TO_TASTE_PATTERNS.search(original)
    qty: Optional[float] = None
    unit: Optional[Unit] = None
    body = original
    if to_taste_match:
        reasons.append("unparsed_qty")
        body = _TO_TASTE_PATTERNS.sub(" ", original).strip()
    else:
        qty, unit, body = _extract_qty_and_unit(original)
        if qty is None and unit is None:
            # No leading number, no unit keyword — still OK;
            # alias lookup may still succeed.
            pass
        if qty is None and unit is None and any(ch.isdigit() for ch in original):
            # Digits present but no known unit; flag.
            reasons.append("unrecognized_qty_pattern")

    head, modifiers = _split_modifiers(body)
    # Normalize and look up.
    normalized = normalize(head)
    index = get_alias_index()
    match = index.lookup(normalized)

    canonical_id: Optional[str] = None
    confidence = 0.0
    if match is not None:
        canonical_id = match.canonical_id
        confidence = match.score
        if match.score < 1.0:
            reasons.append("ambiguous")
    else:
        if normalized:
            reasons.append("unknown")
        else:
            reasons.append("empty_head")

    # Confidence tweaks.
    if canonical_id and not reasons:
        confidence = 1.0
    if canonical_id and "unparsed_qty" in reasons:
        # Found a canonical id despite no qty — still confident on id.
        confidence = max(confidence, 1.0)

    return ParsedIngredient(
        raw=raw,
        qty=qty,
        unit=unit,
        name=head,
        modifiers=modifiers,
        canonical_id=canonical_id,
        confidence=confidence,
        reasons=tuple(reasons),
    )
