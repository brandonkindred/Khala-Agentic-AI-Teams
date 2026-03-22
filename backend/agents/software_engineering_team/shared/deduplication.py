"""
Shared deduplication utilities for semantic near-duplicate removal.

Used by Planning V2 tool agents and Product Requirements Analysis agent
to avoid repeated or near-identical items in merged lists.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Callable, List, TypeVar

T = TypeVar("T")

# Configurable limits to prevent runaway list growth
DEFAULT_MAX_RECOMMENDATIONS = 15
DEFAULT_MAX_ISSUES = 20


def dedupe_strings(
    items: List[str], similarity_threshold: float = 0.85
) -> List[str]:
    """Remove near-duplicate strings from a list based on string similarity.

    Uses SequenceMatcher to detect items that are variations of the same concern.
    Keeps the first occurrence (typically more complete) and discards similar ones.

    The threshold of 0.85 catches obvious duplicates (same sentence with minor word
    changes) while preserving items that follow similar patterns but address
    different topics.

    Args:
        items: List of string items to deduplicate.
        similarity_threshold: Items with similarity >= this value are considered
            duplicates (0.0-1.0).

    Returns:
        Deduplicated list preserving order.
    """
    if not items:
        return items

    unique: List[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        is_duplicate = False
        item_lower = item.lower()
        for existing in unique:
            ratio = SequenceMatcher(None, item_lower, existing.lower()).ratio()
            if ratio >= similarity_threshold:
                is_duplicate = True
                break
        if not is_duplicate:
            unique.append(item)
    return unique


def dedupe_by_key(
    items: List[T],
    key_fn: Callable[[T], str],
    similarity_threshold: float = 0.85,
) -> List[T]:
    """Deduplicate objects by comparing a string extracted via key_fn.

    For each item, key_fn(item) is used as the comparison string. Items whose
    keys are semantically similar (above similarity_threshold) are considered
    duplicates; the first occurrence is kept.

    Args:
        items: List of objects to deduplicate.
        key_fn: Function that extracts a string key from each item for comparison.
        similarity_threshold: Keys with similarity >= this value are considered
            duplicates (0.0-1.0).

    Returns:
        Deduplicated list preserving order.
    """
    if not items:
        return items

    seen_keys: List[str] = []
    unique: List[T] = []
    for item in items:
        key = key_fn(item)
        if not isinstance(key, str):
            unique.append(item)
            continue
        key_lower = key.lower()
        is_duplicate = False
        for existing in seen_keys:
            ratio = SequenceMatcher(None, key_lower, existing).ratio()
            if ratio >= similarity_threshold:
                is_duplicate = True
                break
        if not is_duplicate:
            seen_keys.append(key_lower)
            unique.append(item)
    return unique
