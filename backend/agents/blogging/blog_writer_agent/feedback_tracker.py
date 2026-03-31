"""Track copy-editor feedback across revision iterations.

Provides persistent-issue detection, occurrence counting, and capped
previous-feedback extraction so the writer agent can prioritise issues
that have been flagged multiple times.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from blog_copy_editor_agent.models import FeedbackItem


def _normalise_location(loc: Optional[str]) -> str:
    """Lowercase, strip whitespace, collapse numbers so location strings match across rounds."""
    if not loc:
        return ""
    s = loc.lower().strip()
    s = re.sub(r"\s+", " ", s)
    # "paragraph 3" and "paragraph 4" should still differ, but
    # "Paragraph 3" vs "paragraph 3" should match.
    return s


def _issue_signature(item: "FeedbackItem") -> Tuple[str, str, str]:
    """Canonical key for deduplication: (category, severity, normalised_location)."""
    return (
        (item.category or "").lower().strip(),
        (item.severity or "").lower().strip(),
        _normalise_location(getattr(item, "location", None)),
    )


@dataclass
class PersistentFeedbackItem:
    """A feedback item that has appeared across multiple revision iterations."""

    category: str
    severity: str
    location: Optional[str]
    issue: str
    suggestion: Optional[str]
    occurrence_count: int


@dataclass
class _TrackedEntry:
    """Internal bookkeeping for one issue signature."""

    signature: Tuple[str, str, str]
    occurrence_count: int = 0
    first_seen: int = 0
    last_seen: int = 0
    latest_item: Optional["FeedbackItem"] = None


class FeedbackTracker:
    """Accumulates copy-editor feedback across revision iterations.

    Instead of growing an unbounded list of every ``FeedbackItem`` ever
    produced, this tracker deduplicates by *issue signature* and counts
    how many iterations each issue has appeared in.  It also keeps a
    rolling window of per-iteration signature sets for convergence
    detection.
    """

    def __init__(self, window_size: int = 3) -> None:
        self.window_size = window_size
        self._entries: Dict[Tuple[str, str, str], _TrackedEntry] = {}
        self._iteration_signatures: List[Set[Tuple[str, str, str]]] = []
        self._last_items: List["FeedbackItem"] = []

    def record_iteration(self, iteration: int, items: List["FeedbackItem"]) -> None:
        """Record all feedback items from one copy-editor pass."""
        sigs: Set[Tuple[str, str, str]] = set()
        for item in items:
            sig = _issue_signature(item)
            sigs.add(sig)
            if sig not in self._entries:
                self._entries[sig] = _TrackedEntry(signature=sig, first_seen=iteration)
            entry = self._entries[sig]
            entry.occurrence_count += 1
            entry.last_seen = iteration
            entry.latest_item = item
        self._iteration_signatures.append(sigs)
        self._last_items = list(items)

    def is_stalled(self, threshold: float = 0.80) -> bool:
        """True when the last ``window_size`` rounds share >*threshold* of their issues."""
        if len(self._iteration_signatures) < self.window_size:
            return False
        recent = self._iteration_signatures[-self.window_size :]
        # Jaccard similarity across consecutive pairs; stalled if ALL pairs exceed threshold.
        for i in range(len(recent) - 1):
            a, b = recent[i], recent[i + 1]
            union = a | b
            if not union:
                continue
            jaccard = len(a & b) / len(union)
            if jaccard < threshold:
                return False
        return True

    def get_persistent_issues(self, min_occurrences: int = 2) -> List[PersistentFeedbackItem]:
        """Return deduplicated issues that have been flagged at least *min_occurrences* times."""
        result: List[PersistentFeedbackItem] = []
        for entry in sorted(self._entries.values(), key=lambda e: -e.occurrence_count):
            if entry.occurrence_count < min_occurrences:
                continue
            item = entry.latest_item
            if item is None:
                continue
            result.append(
                PersistentFeedbackItem(
                    category=item.category,
                    severity=item.severity,
                    location=getattr(item, "location", None),
                    issue=item.issue,
                    suggestion=getattr(item, "suggestion", None),
                    occurrence_count=entry.occurrence_count,
                )
            )
        return result

    def get_capped_previous_feedback(self, max_items: int = 15) -> List["FeedbackItem"]:
        """Return a bounded set of previous feedback for the revision prompt.

        Prioritises persistent issues (by occurrence count), then pads with
        the most recent round's items up to *max_items*.
        """
        # Start with persistent issues (most-flagged first).
        persistent_sigs: Set[Tuple[str, str, str]] = set()
        result: List["FeedbackItem"] = []
        for entry in sorted(self._entries.values(), key=lambda e: -e.occurrence_count):
            if entry.occurrence_count < 2 or entry.latest_item is None:
                continue
            if len(result) >= max_items:
                break
            result.append(entry.latest_item)
            persistent_sigs.add(entry.signature)

        # Fill remaining slots with most-recent-round items not already covered.
        for item in self._last_items:
            if len(result) >= max_items:
                break
            sig = _issue_signature(item)
            if sig not in persistent_sigs:
                result.append(item)
                persistent_sigs.add(sig)

        return result
