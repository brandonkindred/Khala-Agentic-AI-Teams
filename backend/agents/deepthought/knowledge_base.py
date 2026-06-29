"""Thread-safe shared knowledge base for cross-agent deduplication and reuse.

Every agent in the recursive tree can write findings and read what siblings/
cousins have already discovered.  The orchestrator owns the instance and
passes it down through the tree.
"""

from __future__ import annotations

import hashlib
import logging
import threading

from deepthought.models import KnowledgeEntry

logger = logging.getLogger(__name__)

# Similarity threshold for fuzzy question matching (0-1).  Two focus
# questions with a normalised overlap above this are considered duplicates.
_SIMILARITY_THRESHOLD = 0.70


def _normalise(text: str) -> set[str]:
    """Cheap bag-of-words normalisation for similarity checks."""
    return {w.lower().strip("?.,!;:") for w in text.split() if len(w) > 2}


def _similarity(a: str, b: str) -> float:
    """Jaccard similarity between two strings."""
    sa, sb = _normalise(a), _normalise(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


class SharedKnowledgeBase:
    """Centralised, thread-safe store of findings for one Deepthought run."""

    def __init__(self) -> None:
        self._entries: list[KnowledgeEntry] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add(self, entry: KnowledgeEntry) -> None:
        """Store a finding.  Thread-safe."""
        with self._lock:
            self._entries.append(entry)
            logger.debug(
                "Knowledge added by %s: %.80s (tags=%s)",
                entry.agent_name,
                entry.finding,
                entry.tags,
            )

    # ------------------------------------------------------------------
    # Read / Query
    # ------------------------------------------------------------------

    def find_similar(
        self, question: str, threshold: float = _SIMILARITY_THRESHOLD
    ) -> list[KnowledgeEntry]:
        """Return entries whose focus_question is similar to *question*."""
        with self._lock:
            return [
                e for e in self._entries if _similarity(e.focus_question, question) >= threshold
            ]

    def find_by_tags(self, tags: list[str]) -> list[KnowledgeEntry]:
        """Return entries sharing at least one tag."""
        tag_set = set(tags)
        with self._lock:
            return [e for e in self._entries if tag_set & set(e.tags)]

    def all_entries(self) -> list[KnowledgeEntry]:
        """Return a snapshot of all entries."""
        with self._lock:
            return list(self._entries)

    def summary_for_prompt(self, max_chars: int = 4000) -> str:
        """Render a concise text summary of all findings for prompt injection."""
        with self._lock:
            if not self._entries:
                return "(No prior findings.)"
            parts: list[str] = []
            total = 0
            for e in self._entries:
                line = f"- [{e.agent_name}] {e.finding[:200]}"
                if total + len(line) > max_chars:
                    parts.append(
                        f"... and {len(self._entries) - len(parts)} more entries (truncated)"
                    )
                    break
                parts.append(line)
                total += len(line)
            return "\n".join(parts)

    def cache_key(self, question: str) -> str:
        """Deterministic hash for a focus question (for result caching)."""
        return hashlib.sha256(question.strip().lower().encode()).hexdigest()[:16]
