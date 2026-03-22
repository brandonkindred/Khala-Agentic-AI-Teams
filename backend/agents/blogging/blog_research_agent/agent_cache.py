"""
Cache module for ResearchAgent checkpointing and resume capability.

Stores intermediate results at each step so the agent can resume from the last
completed step after a failure.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from .models import (
    ResearchBriefInput,
)

logger = logging.getLogger(__name__)


class AgentCacheState(BaseModel):
    """Serializable state for agent checkpoint."""

    # Step 1: Parsed brief
    normalized: Optional[Dict[str, Any]] = None

    # Step 2: Generated queries
    queries: Optional[List[Dict[str, Any]]] = None

    # Step 3: Search candidates
    candidates: Optional[List[Dict[str, Any]]] = None

    # Step 4: Fetched documents
    documents: Optional[List[Dict[str, Any]]] = None

    # Step 5: Scored documents (list of [doc_dict, score, type_label] for JSON serialization)
    scored_docs: Optional[List[List[Any]]] = None

    # Step 6: Summarized references
    references: Optional[List[Dict[str, Any]]] = None

    # Step 7: Final notes
    notes: Optional[str] = None

    # Metadata
    brief_input: Dict[str, Any]  # Original brief input for validation
    last_completed_step: Optional[str] = None  # Name of last completed step


class AgentCache:
    """
    File-based cache for ResearchAgent checkpoints.

    Preconditions:
        - cache_dir is a valid directory path (will be created if needed).
    Invariants:
        - Cache files are stored as JSON in cache_dir.
    """

    def __init__(self, cache_dir: str | Path = ".agent_cache") -> None:
        """
        Preconditions:
            - cache_dir is a valid path string or Path.
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        logger.debug("AgentCache initialized with cache_dir=%s", self.cache_dir)

    def _cache_key(self, brief_input: ResearchBriefInput) -> str:
        """
        Generate a cache key from the brief input.

        Preconditions: brief_input is valid.
        Postconditions: Returns a deterministic hash string.
        """
        # Create a stable representation of the input
        key_data = {
            "brief": brief_input.brief,
            "audience": brief_input.audience,
            "tone_or_purpose": brief_input.tone_or_purpose,
            "max_results": brief_input.max_results,
            "per_query_limit": brief_input.per_query_limit,
            "recency_preference": brief_input.recency_preference,
        }
        key_str = json.dumps(key_data, sort_keys=True)
        return hashlib.sha256(key_str.encode()).hexdigest()[:16]

    def _cache_file(self, cache_key: str) -> Path:
        """Get the cache file path for a given cache key."""
        return self.cache_dir / f"{cache_key}.json"

    def save_checkpoint(
        self,
        brief_input: ResearchBriefInput,
        step_name: str,
        **kwargs: Any,
    ) -> None:
        """
        Save a checkpoint after completing a step.

        Preconditions:
            - brief_input is valid.
            - step_name is one of: "normalized", "queries", "candidates", "documents",
              "scored_docs", "references", "notes".
            - kwargs contains the appropriate data for the step.
        Postconditions:
            - Cache file is written with updated state.
        """
        cache_key = self._cache_key(brief_input)
        cache_file = self._cache_file(cache_key)

        # Load existing state or create new
        if cache_file.exists():
            try:
                state_data = json.loads(cache_file.read_text())
                state = AgentCacheState(**state_data)
            except Exception as e:
                logger.warning("Failed to load existing cache, creating new: %s", e)
                state = AgentCacheState(brief_input=brief_input.model_dump())
        else:
            state = AgentCacheState(brief_input=brief_input.model_dump())

        # Update state based on step
        if step_name == "normalized" and "normalized" in kwargs:
            state.normalized = kwargs["normalized"]
        elif step_name == "queries" and "queries" in kwargs:
            queries = kwargs["queries"]
            state.queries = [q.model_dump() if hasattr(q, "model_dump") else q for q in queries]
        elif step_name == "candidates" and "candidates" in kwargs:
            candidates = kwargs["candidates"]
            state.candidates = [
                c.model_dump() if hasattr(c, "model_dump") else c for c in candidates
            ]
        elif step_name == "documents" and "documents" in kwargs:
            documents = kwargs["documents"]
            state.documents = [d.model_dump() if hasattr(d, "model_dump") else d for d in documents]
        elif step_name == "scored_docs" and "scored_docs" in kwargs:
            scored_docs = kwargs["scored_docs"]
            # Serialize as [doc_dict, relevance, authority, accuracy, type_label]
            state.scored_docs = []
            for t in scored_docs:
                if len(t) >= 5:
                    doc, rel, auth, acc, type_label = t[0], t[1], t[2], t[3], t[4]
                    state.scored_docs.append([
                        doc.model_dump() if hasattr(doc, "model_dump") else doc,
                        rel, auth, acc, type_label,
                    ])
                else:
                    doc, score, type_label = t[0], t[1], t[2] if len(t) > 2 else None
                    state.scored_docs.append([
                        doc.model_dump() if hasattr(doc, "model_dump") else doc,
                        score, 0.5, 0.5, type_label,
                    ])
        elif step_name == "references" and "references" in kwargs:
            references = kwargs["references"]
            state.references = [
                r.model_dump() if hasattr(r, "model_dump") else r for r in references
            ]
        elif step_name == "notes" and "notes" in kwargs:
            state.notes = kwargs["notes"]

        state.last_completed_step = step_name

        # Write to file
        cache_file.write_text(state.model_dump_json(indent=2))
        logger.info("Saved checkpoint: step=%s, cache_key=%s", step_name, cache_key)

    def load_checkpoint(self, brief_input: ResearchBriefInput) -> Optional[AgentCacheState]:
        """
        Load the latest checkpoint for a given brief input.

        Preconditions: brief_input is valid.
        Postconditions:
            - Returns AgentCacheState if cache exists and matches brief_input, else None.
        """
        cache_key = self._cache_key(brief_input)
        cache_file = self._cache_file(cache_key)

        if not cache_file.exists():
            logger.debug("No cache found for cache_key=%s", cache_key)
            return None

        try:
            state_data = json.loads(cache_file.read_text())
            state = AgentCacheState(**state_data)

            # Validate that the brief input matches
            cached_brief = ResearchBriefInput(**state.brief_input)
            if (
                cached_brief.brief != brief_input.brief
                or cached_brief.audience != brief_input.audience
                or cached_brief.tone_or_purpose != brief_input.tone_or_purpose
                or cached_brief.max_results != brief_input.max_results
                or cached_brief.per_query_limit != brief_input.per_query_limit
                or cached_brief.recency_preference != brief_input.recency_preference
            ):
                logger.warning(
                    "Cache exists but brief input doesn't match, ignoring cache for cache_key=%s",
                    cache_key,
                )
                return None

            logger.info(
                "Loaded checkpoint: last_step=%s, cache_key=%s",
                state.last_completed_step,
                cache_key,
            )
            return state
        except Exception as e:
            logger.warning("Failed to load cache file %s: %s", cache_file, e)
            return None

    def clear_checkpoint(self, brief_input: ResearchBriefInput) -> None:
        """
        Clear the cache for a given brief input.

        Preconditions: brief_input is valid.
        Postconditions: Cache file is deleted if it exists.
        """
        cache_key = self._cache_key(brief_input)
        cache_file = self._cache_file(cache_key)
        if cache_file.exists():
            cache_file.unlink()
            logger.info("Cleared cache for cache_key=%s", cache_key)
