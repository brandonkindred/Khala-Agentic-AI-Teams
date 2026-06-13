"""Turn a job-seeker profile into targeted web search queries."""

from __future__ import annotations

import logging
from typing import List, Optional

from ..profile.model import JobSeekerProfile
from ..prompts import QUERY_BUILDER_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class QueryBuilderAgent:
    """Build a bounded set of search queries from the seeker's criteria.

    Uses the LLM when available, with a deterministic template fallback so the
    pipeline (and its unit tests) work fully offline.

    Invariants:
        * :meth:`build` never returns more than ``max_queries`` queries and
          never returns an empty list when the profile has any target title.
    """

    def __init__(self, llm_client: Optional[object] = None) -> None:
        self._llm = llm_client

    def _client(self):  # noqa: ANN202
        if self._llm is None:
            from llm_service import get_client

            self._llm = get_client(agent_key="job_matching.query_builder")
        return self._llm

    def build(self, profile: JobSeekerProfile, *, max_queries: int = 6) -> List[str]:
        """Return up to ``max_queries`` search query strings.

        Preconditions:
            * ``max_queries >= 1``.
        Postconditions:
            * Result length is in ``[0, max_queries]`` (0 only when the profile
              has no titles and no keywords); entries are unique and non-empty.
        """
        assert max_queries >= 1, "max_queries must be at least 1"

        queries = self._build_via_llm(profile, max_queries)
        if not queries:
            queries = self._build_fallback(profile, max_queries)

        seen: set[str] = set()
        deduped: List[str] = []
        for q in queries:
            q = (q or "").strip()
            if q and q.lower() not in seen:
                seen.add(q.lower())
                deduped.append(q)
        return deduped[:max_queries]

    def _build_via_llm(self, profile: JobSeekerProfile, max_queries: int) -> List[str]:
        try:
            prompt = (
                f"Job seeker criteria (JSON):\n{profile.model_dump_json(indent=2)}\n\n"
                f"Produce at most {max_queries} search queries."
            )
            data = self._client().complete_json(
                prompt,
                temperature=0.3,
                system_prompt=QUERY_BUILDER_SYSTEM_PROMPT,
                objective="build job search queries",
            )
            raw = data.get("queries", []) if isinstance(data, dict) else []
            # The model occasionally returns a single string instead of a list;
            # treat that as one query rather than iterating it character-by-char.
            if isinstance(raw, str):
                raw = [raw]
            elif not isinstance(raw, list):
                raw = []
            return [str(q) for q in raw if str(q).strip()]
        except Exception:  # noqa: BLE001 - LLM is best-effort; fall back deterministically
            logger.warning("Query builder LLM call failed; using template fallback", exc_info=True)
            return []

    def _build_fallback(self, profile: JobSeekerProfile, max_queries: int) -> List[str]:
        """Deterministic query construction used when the LLM is unavailable."""
        titles = profile.target_titles or profile.keywords or ["software engineer"]
        locations = list(profile.locations)
        if profile.remote_preference in ("remote", "hybrid"):
            locations = [profile.remote_preference] + locations
        if not locations:
            locations = [""]

        queries: List[str] = []
        for title in titles:
            for loc in locations:
                parts = [title, loc, "jobs hiring"]
                queries.append(" ".join(p for p in parts if p).strip())
                if len(queries) >= max_queries:
                    return queries
        for company in profile.preferred_companies:
            queries.append(f"{company} careers {titles[0]} open roles")
            if len(queries) >= max_queries:
                break
        return queries
