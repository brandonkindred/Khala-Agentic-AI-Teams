"""Discover and normalize open roles from the web."""

from __future__ import annotations

import logging
from typing import List, Optional

from ..models import JobPosting
from ..prompts import POSTING_EXTRACTION_SYSTEM_PROMPT
from ..tools.web_fetch import WebFetcher, WebFetchError
from ..tools.web_search import OllamaWebSearch, SearchResult

logger = logging.getLogger(__name__)

_VALID_REMOTE = {"remote", "hybrid", "onsite", "unknown"}


class JobScannerAgent:
    """Run web searches, fetch listing pages, and extract structured postings.

    Invariants:
        * :meth:`scan` never returns two postings sharing a fingerprint.
        * The number of postings returned never exceeds ``max_roles``.
    """

    def __init__(
        self,
        *,
        llm_client: Optional[object] = None,
        searcher: Optional[OllamaWebSearch] = None,
        fetcher: Optional[WebFetcher] = None,
    ) -> None:
        self._llm = llm_client
        self._searcher = searcher
        self._fetcher = fetcher

    def _client(self):  # noqa: ANN202
        if self._llm is None:
            from llm_service import get_client

            self._llm = get_client(agent_key="job_matching.scanner")
        return self._llm

    @property
    def searcher(self) -> OllamaWebSearch:
        if self._searcher is None:
            self._searcher = OllamaWebSearch()
        return self._searcher

    @property
    def fetcher(self) -> WebFetcher:
        if self._fetcher is None:
            self._fetcher = WebFetcher()
        return self._fetcher

    def scan(
        self,
        queries: List[str],
        *,
        max_roles: int = 40,
        results_per_query: int = 10,
        skip_fingerprints: Optional[set[str]] = None,
    ) -> List[JobPosting]:
        """Search, fetch, and extract up to ``max_roles`` unique postings.

        Preconditions:
            * ``max_roles >= 1`` and ``results_per_query >= 1``.
        Postconditions:
            * Returns a fingerprint-deduplicated list of length ``<= max_roles``;
              fingerprints in ``skip_fingerprints`` are excluded.
        """
        assert max_roles >= 1, "max_roles must be at least 1"
        assert results_per_query >= 1, "results_per_query must be at least 1"
        skip = skip_fingerprints or set()

        seen: set[str] = set()
        postings: List[JobPosting] = []
        for hit in self._gather_hits(queries, results_per_query):
            if len(postings) >= max_roles:
                break
            posting = self._extract_posting(hit)
            if posting is None:
                continue
            posting.ensure_fingerprint()
            fp = posting.fingerprint
            if fp in seen or fp in skip:
                continue
            seen.add(fp)
            postings.append(posting)
        return postings

    def _gather_hits(self, queries: List[str], results_per_query: int) -> List[SearchResult]:
        """Run each query and return de-duplicated search hits (by URL)."""
        seen_urls: set[str] = set()
        hits: List[SearchResult] = []
        for query in queries:
            try:
                results = self.searcher.search(query, max_results=results_per_query)
            except Exception:  # noqa: BLE001 - one bad query shouldn't abort the scan
                logger.warning("Search failed for query %r", query, exc_info=True)
                continue
            for r in results:
                if r.url and r.url not in seen_urls:
                    seen_urls.add(r.url)
                    hits.append(r)
        return hits

    def _extract_posting(self, hit: SearchResult) -> Optional[JobPosting]:
        """Fetch the hit's page and LLM-extract a posting, or None if not a role."""
        page_text = hit.snippet
        try:
            page = self.fetcher.fetch(hit.url)
            if page.text:
                page_text = page.text
        except WebFetchError:
            logger.info("Could not fetch %s; falling back to snippet", hit.url)

        if not page_text.strip():
            return None

        prompt = (
            f"Source URL: {hit.url}\nSearch title: {hit.title}\n\nPage text:\n{page_text[:8000]}"
        )
        try:
            data = self._client().complete_json(
                prompt,
                temperature=0.0,
                system_prompt=POSTING_EXTRACTION_SYSTEM_PROMPT,
                objective="scan job posting",
            )
        except Exception:  # noqa: BLE001 - extraction is best-effort per page
            logger.warning("Posting extraction failed for %s", hit.url, exc_info=True)
            return None

        if not isinstance(data, dict) or not _as_bool(data.get("is_job_posting")):
            return None

        remote = str(data.get("remote_mode") or "unknown").lower()
        if remote not in _VALID_REMOTE:
            remote = "unknown"

        return JobPosting(
            title=str(data.get("title") or hit.title or "").strip(),
            company=str(data.get("company") or "").strip(),
            location=str(data.get("location") or "").strip(),
            remote_mode=remote,  # type: ignore[arg-type]
            salary_min=_as_int(data.get("salary_min")),
            salary_max=_as_int(data.get("salary_max")),
            currency=str(data.get("currency") or "USD").strip() or "USD",
            url=hit.url,
            source=hit.source,
            description=str(data.get("description") or "").strip(),
            posted_at=(str(data["posted_at"]) if data.get("posted_at") else None),
        )


def _as_bool(value: object) -> bool:
    """Strictly interpret an LLM-supplied flag as a boolean.

    LLM JSON frequently stringifies booleans (``"false"``), which a bare
    truthiness check would wrongly treat as ``True``. Only genuine truthy
    values pass.

    Postconditions:
        * Returns ``True`` only for ``True``, the strings ``"true"``/``"1"``/
          ``"yes"`` (case-insensitive), or the number ``1``; ``False`` otherwise.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    if isinstance(value, (int, float)):
        return value == 1
    return False


def _as_int(value: object) -> Optional[int]:
    """Coerce ``value`` to int, returning None on failure/empty."""
    if value is None or value == "":
        return None
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
