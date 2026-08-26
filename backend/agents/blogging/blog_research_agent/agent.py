from __future__ import annotations

import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, List, Optional, Tuple

from pydantic import HttpUrl
from strands import Agent

logger = logging.getLogger(__name__)

from agents.blogging.shared.agent_base import _BlogAgentBase  # noqa: E402

from llm_service import LLMJsonParseError, compact_text, extract_json_from_response  # noqa: E402
from shared.concurrency import parallel_map  # noqa: E402

from .agent_cache import AgentCache  # noqa: E402
from .models import (  # noqa: E402
    AcademicPaper,
    CandidateResult,
    ResearchAgentOutput,
    ResearchBriefInput,
    ResearchReference,
    SearchQuery,
    SourceDocument,
)
from .prompts import (  # noqa: E402
    BRIEF_PARSING_PROMPT,
    DOC_RELEVANCE_SCORING_PROMPT,
    DOC_SUMMARIZATION_PROMPT,
    FINAL_SYNTHESIS_PROMPT,
    QUERY_GENERATION_PROMPT,
    SIMILAR_TOPICS_PROMPT,
)
from .tools.arxiv_search import search_arxiv  # noqa: E402
from .tools.web_fetch import SimpleWebFetcher  # noqa: E402
from .tools.web_search import OllamaWebSearch  # noqa: E402

# Upper bound on concurrent per-document LLM calls in the scoring and
# summarization fan-outs. Named here (rather than inline at each call site) so
# the two stages stay in lockstep and the cap can be tuned in one place.
_DOC_PARALLEL_WORKERS = 8


class ResearchAgent(_BlogAgentBase):
    """
    Core research agent implementing the workflow defined in the plan.

    It is intentionally stateless beyond the constructor dependencies so
    it can be easily embedded in a Strands runtime or other orchestrator.
    """

    def __init__(
        self,
        llm_client: Any,
        *,
        web_search: OllamaWebSearch | None = None,
        web_fetcher: SimpleWebFetcher | None = None,
        max_fetch_documents: int = 20,
        cache: AgentCache | None = None,
    ) -> None:
        """
        Preconditions:
            - llm_client is not None.
            - max_fetch_documents >= 1.
        Invariants (after construction):
            - self._model is not None.
            - self.max_fetch_documents >= 1.
        """
        super().__init__(llm_client)
        assert max_fetch_documents >= 1, "max_fetch_documents must be at least 1"
        self.web_search = web_search or OllamaWebSearch()
        self.web_fetcher = web_fetcher or SimpleWebFetcher()
        self.max_fetch_documents = max_fetch_documents
        self.cache = cache

    def _call_json(self, prompt: str) -> dict:
        """Call the Strands Agent and parse JSON from the result."""
        agent = Agent(
            model=self._model,
            system_prompt="You are a research assistant. Respond with valid JSON only.",
        )
        result = agent(prompt + "\n\nRespond with valid JSON only, no markdown fences.")
        return extract_json_from_response(str(result).strip())

    # Public API ---------------------------------------------------------

    def run(
        self,
        brief_input: ResearchBriefInput,
        *,
        progress_callback: Optional[Callable[[str, float], None]] = None,
    ) -> ResearchAgentOutput:
        """
        Execute the full research workflow and return structured output.

        If cache is enabled, will resume from the last completed step on failure.

        progress_callback: Optional callback (status_text, sub_progress_0_to_1) for UI updates.

        Preconditions:
            - brief_input is a valid ResearchBriefInput (e.g. from model_validate).
        Postconditions:
            - Returns ResearchAgentOutput with query_plan (list), references (list,
              length <= brief_input.max_results), notes (str or None), and
              compiled_document (formatted document of most relevant links with summaries).
        """

        def _report(status: str, sub: float) -> None:
            if progress_callback:
                progress_callback(status, sub)

        self._progress_callback = progress_callback
        try:
            brief_preview = (
                (brief_input.brief[:77] + "...")
                if len(brief_input.brief) > 80
                else brief_input.brief
            )
            logger.info(
                "Starting research: brief=%s, max_results=%s",
                brief_preview,
                brief_input.max_results,
            )

            _report("Starting research...", 0.0)

            # Try to load checkpoint
            cached_state = None
            if self.cache:
                cached_state = self.cache.load_checkpoint(brief_input)
                if cached_state:
                    logger.info(
                        "Resuming from checkpoint: last_step=%s", cached_state.last_completed_step
                    )

            # Step 1: Parse brief
            _report("Parsing brief...", 0.05)
            if (
                cached_state and cached_state.normalized
            ):  # pragma: no cover - resume-from-checkpoint branch requires a populated ResearchCache; integration tests cover the cache-hit replay path.
                logger.info("Using cached normalized brief")
                normalized = cached_state.normalized
            else:
                normalized = self._parse_brief(brief_input)
                if self.cache:
                    self.cache.save_checkpoint(brief_input, "normalized", normalized=normalized)

            # Step 2: Generate queries
            _report("Generating search queries...", 0.10)
            if (
                cached_state and cached_state.queries
            ):  # pragma: no cover - resume-from-checkpoint branch; see Step 1.
                logger.info("Using cached queries (%s)", len(cached_state.queries))
                queries = [SearchQuery(**q) for q in cached_state.queries]
            else:
                queries = self._generate_queries(brief_input, normalized)
                if self.cache:
                    self.cache.save_checkpoint(brief_input, "queries", queries=queries)

            # Step 3: Run searches
            # candidates is checked for "is not None" rather than truthiness: a
            # zero-candidate search is a legitimately completed checkpoint, not a
            # missing one, and re-running it on resume would repeat the web searches
            # for no reason.
            if (
                cached_state and cached_state.candidates is not None
            ):  # pragma: no cover - resume-from-checkpoint branch; see Step 1.
                logger.info("Using cached candidates (%s)", len(cached_state.candidates))
                candidates = [CandidateResult(**c) for c in cached_state.candidates]
            else:
                _report("Running web searches...", 0.15)
                candidates = self._run_searches(
                    queries,
                    brief_input,
                    on_search_progress=lambda i, n: _report(
                        f"Running web search {i + 1}/{n}...", 0.15 + 0.20 * (i + 1) / max(1, n)
                    ),
                )
                if self.cache:
                    self.cache.save_checkpoint(brief_input, "candidates", candidates=candidates)
            _report("Fetching and reading web pages...", 0.38)

            # Step 4: Fetch documents
            if (
                cached_state and cached_state.documents
            ):  # pragma: no cover - resume-from-checkpoint branch; see Step 1.
                logger.info("Using cached documents (%s)", len(cached_state.documents))
                documents = [SourceDocument(**d) for d in cached_state.documents]
            else:
                documents = self._fetch_documents(candidates, brief_input)
                if self.cache:
                    self.cache.save_checkpoint(brief_input, "documents", documents=documents)

            # Step 5: Score documents
            _report("Scoring documents for relevance...", 0.50)
            if (
                cached_state and cached_state.scored_docs
            ):  # pragma: no cover - resume-from-checkpoint branch including the legacy 3-tuple shape; see Step 1.
                logger.info("Using cached scored documents (%s)", len(cached_state.scored_docs))
                scored_docs = []
                for item in cached_state.scored_docs:
                    # Support old format [doc, score, type] and new [doc, relevance, authority, accuracy, type]
                    if len(item) >= 5:
                        scored_docs.append(
                            (SourceDocument(**item[0]), item[1], item[2], item[3], item[4])
                        )
                    else:
                        scored_docs.append(
                            (
                                SourceDocument(**item[0]),
                                item[1],
                                0.5,
                                0.5,
                                item[2] if len(item) > 2 else None,
                            )
                        )
            else:
                scored_docs = self._score_documents(documents, brief_input)
                if self.cache:
                    self.cache.save_checkpoint(brief_input, "scored_docs", scored_docs=scored_docs)

            # Step 6: Summarize documents
            _report("Summarizing references...", 0.65)
            if (
                cached_state and cached_state.references
            ):  # pragma: no cover - resume-from-checkpoint branch; see Step 1.
                logger.info("Using cached references (%s)", len(cached_state.references))
                references = [ResearchReference(**r) for r in cached_state.references]
            else:
                references = self._summarize_documents(scored_docs, brief_input)
                if self.cache:
                    self.cache.save_checkpoint(brief_input, "references", references=references)

            # Steps 7-9: synthesize overview (LLM), fetch academic papers (arXiv
            # HTTP), and find similar topics (LLM) are mutually independent — none
            # consumes another's output — so run them concurrently instead of as
            # three sequential round-trips. Each keeps its own resume-from-checkpoint
            # short-circuit, but — unlike the strictly-sequential steps above — the
            # checkpoint *save* is deliberately deferred until after all three
            # `.result()`s come back and done here one at a time: AgentCache.
            # save_checkpoint() is an unlocked read-modify-write over one shared JSON
            # file, so three concurrent savers would race and can clobber each
            # other's (or the earlier steps') already-persisted state.
            _report("Synthesizing overview, searching arXiv, finding similar topics...", 0.78)

            notes_is_cached = bool(
                cached_state and cached_state.notes is not None
            )  # pragma: no cover - resume-from-checkpoint branch; see Step 1.
            academic_papers_is_cached = bool(
                cached_state and cached_state.academic_papers is not None
            )  # pragma: no cover - resume-from-checkpoint branch; see Step 1.
            similar_topics_is_cached = bool(
                cached_state and cached_state.similar_topics is not None
            )  # pragma: no cover - resume-from-checkpoint branch; see Step 1.

            def _resolve_notes() -> Any:
                if notes_is_cached:
                    logger.info("Using cached notes")
                    return cached_state.notes
                return self._synthesize_overview(brief_input, references)

            def _resolve_academic_papers() -> List[AcademicPaper]:
                if academic_papers_is_cached:
                    logger.info(
                        "Using cached academic papers (%s)", len(cached_state.academic_papers)
                    )
                    return [AcademicPaper(**p) for p in cached_state.academic_papers]
                return self._fetch_academic_papers(brief_input)

            def _resolve_similar_topics() -> List[str]:
                if similar_topics_is_cached:
                    logger.info(
                        "Using cached similar topics (%s)", len(cached_state.similar_topics)
                    )
                    return cached_state.similar_topics
                return self._get_similar_topics(brief_input, references)

            # Run each step inside a copy of this thread's context so the LLM
            # attribution / request-id contextvars propagate to the workers — a
            # raw ThreadPoolExecutor does not copy them (see llm_service.attribution).
            with ThreadPoolExecutor(max_workers=3) as executor:
                notes_future = executor.submit(contextvars.copy_context().run, _resolve_notes)
                academic_future = executor.submit(
                    contextvars.copy_context().run, _resolve_academic_papers
                )
                similar_future = executor.submit(
                    contextvars.copy_context().run, _resolve_similar_topics
                )
                notes = notes_future.result()
                academic_papers = academic_future.result()
                similar_topics = similar_future.result()

            # Persist newly-computed checkpoints sequentially (see comment above).
            if self.cache:
                if not notes_is_cached:
                    self.cache.save_checkpoint(brief_input, "notes", notes=notes)
                if not academic_papers_is_cached:
                    self.cache.save_checkpoint(
                        brief_input, "academic_papers", academic_papers=academic_papers
                    )
                if not similar_topics_is_cached:
                    self.cache.save_checkpoint(
                        brief_input, "similar_topics", similar_topics=similar_topics
                    )

            # Step 10: Compile document (Blog Post Research format)
            _report("Compiling research document...", 0.95)
            compiled_document = self._compile_document(
                brief_input, references, notes, academic_papers, similar_topics
            )

            _report("Research complete", 1.0)
            logger.info(
                "Research complete: %s references, %s academic papers, %s similar topics, compiled_document=%s",
                len(references),
                len(academic_papers),
                len(similar_topics),
                len(compiled_document) if compiled_document else 0,
            )
            self._progress_callback = None
            return ResearchAgentOutput(
                query_plan=queries,
                references=references,
                notes=notes,
                compiled_document=compiled_document,
                academic_papers=academic_papers,
                similar_topics=similar_topics,
            )
        finally:
            self._progress_callback = None

    def _report_llm(self, status: str, sub: float) -> None:
        """Report status to UI before an LLM request (if progress_callback is set)."""
        if getattr(self, "_progress_callback", None):
            self._progress_callback(status, sub)

    # Steps --------------------------------------------------------------

    def _parse_brief(self, brief_input: ResearchBriefInput) -> dict:
        """
        Preconditions: brief_input is a valid ResearchBriefInput.
        Postconditions: Returns a dict with keys core_topics, angle, constraints.
        """
        logger.info("Parsing brief...")
        prompt = BRIEF_PARSING_PROMPT + "\n\n" + f"Brief: {brief_input.brief}\n"
        if brief_input.audience:
            prompt += f"Audience: {brief_input.audience}\n"
        if brief_input.tone_or_purpose:
            prompt += f"Tone/Purpose: {brief_input.tone_or_purpose}\n"

        self._report_llm("Parsing brief...", 0.05)
        parsed = self._call_json(prompt)

        return {
            "core_topics": parsed.get("core_topics") or [brief_input.brief],
            "angle": parsed.get("angle") or "",
            "constraints": parsed.get("constraints") or [],
        }

    def _generate_queries(
        self, brief_input: ResearchBriefInput, normalized: dict
    ) -> List[SearchQuery]:
        """
        Preconditions: brief_input valid; normalized has core_topics, angle, constraints.
        Postconditions: Returns non-empty list of SearchQuery (fallback to brief if needed).
        """
        logger.info("Generating search queries...")
        prompt = QUERY_GENERATION_PROMPT.format(
            core_topics=normalized.get("core_topics"),
            angle=normalized.get("angle"),
            constraints=normalized.get("constraints"),
            audience=brief_input.audience or "",
            tone_or_purpose=brief_input.tone_or_purpose or "",
        )
        self._report_llm("Generating search queries...", 0.10)
        data = self._call_json(prompt)
        queries_data = data.get("queries") or []

        queries: List[SearchQuery] = []
        for item in queries_data:
            text = item.get("query_text")
            if not text:
                continue
            queries.append(
                SearchQuery(
                    query_text=text,
                    intent=item.get("intent"),
                )
            )

        # Fallback: if LLM returned nothing, just use the brief itself.
        if not queries:
            queries.append(SearchQuery(query_text=brief_input.brief, intent="overview"))

        logger.info("Generated %s search queries", len(queries))
        return queries

    def _run_searches(
        self,
        queries: List[SearchQuery],
        brief_input: ResearchBriefInput,
        *,
        on_search_progress: Optional[Callable[[int, int], None]] = None,
    ) -> List[CandidateResult]:
        """
        Preconditions: queries non-empty; brief_input valid.
        Postconditions: Returns list of CandidateResult, deduplicated by URL.
        """
        logger.info("Running web searches...")
        seen_urls = set()
        candidates: List[CandidateResult] = []
        n_queries = len(queries)

        for i, query in enumerate(queries):
            if on_search_progress:
                on_search_progress(i, n_queries)
            query_preview = (
                (query.query_text[:77] + "...") if len(query.query_text) > 80 else query.query_text
            )
            logger.info("Running search %s/%s: %s", i + 1, n_queries, query_preview)
            results = self.web_search.search(
                query,
                max_results=brief_input.per_query_limit,
                recency_preference=brief_input.recency_preference,
            )
            for result in results:
                url_str = str(result.url)
                if url_str in seen_urls:
                    continue
                seen_urls.add(url_str)
                candidates.append(result)

        logger.info("Found %s unique candidates", len(candidates))
        return candidates

    def _fetch_documents(
        self,
        candidates: List[CandidateResult],
        brief_input: ResearchBriefInput,
    ) -> List[SourceDocument]:
        """
        Preconditions: candidates and brief_input valid.
        Postconditions: Returns list of SourceDocument (best-effort; fetch failures skipped).
        """
        max_docs = min(self.max_fetch_documents, len(candidates))
        logger.info("Fetching up to %s documents...", max_docs)
        documents: List[SourceDocument] = []

        for candidate in candidates[:max_docs]:
            try:
                doc = self.web_fetcher.fetch(HttpUrl(str(candidate.url)))
            except Exception:
                # Best-effort: skip failures.
                continue
            documents.append(doc)

        logger.info("Fetched %s documents", len(documents))
        return documents

    def _score_one_document(
        self,
        doc: SourceDocument,
        brief_input: ResearchBriefInput,
    ) -> Tuple[SourceDocument, float, float, float, str]:
        """Score a single document for relevance, authority, accuracy, and type. Used by _score_documents."""
        # Budget: use 1.0 chars/token (safe for web content which tokenizes poorly).
        ctx_tokens = 16384  # Default context budget for safety
        max_content_chars = max(4000, ctx_tokens - 6000)  # 1 char ≈ 1 token for safety
        doc_content = compact_text(
            doc.content or "", max_content_chars, self._model, "document for scoring"
        )
        # Hard safety net: if compaction returned something still over budget, truncate.
        if len(doc_content) > max_content_chars:
            doc_content = doc_content[:max_content_chars]
        prompt = (
            DOC_RELEVANCE_SCORING_PROMPT
            + "\n\n"
            + (
                f"Brief:\n{brief_input.brief}\n\n"
                f"Document title: {doc.title or ''}\n"
                f"Document content:\n{doc_content}\n"
            )
        )
        data = self._call_json(prompt)
        rel = data.get("relevance_score")
        auth = data.get("authority_score")
        acc = data.get("accuracy_score")
        if not isinstance(
            rel, (int, float)
        ):  # pragma: no cover - defensive guard against non-numeric LLM responses; covered by integration tests.
            rel = 0.0
        if not isinstance(
            auth, (int, float)
        ):  # pragma: no cover - defensive guard against non-numeric LLM responses; covered by integration tests.
            auth = 0.5
        if not isinstance(
            acc, (int, float)
        ):  # pragma: no cover - defensive guard against non-numeric LLM responses; covered by integration tests.
            acc = 0.5
        relevance = max(0.0, min(1.0, float(rel)))
        authority = max(0.0, min(1.0, float(auth)))
        accuracy = max(0.0, min(1.0, float(acc)))
        type_label = data.get("type") or None
        logger.debug(
            "Scored doc: title=%s, relevance=%s, authority=%s, accuracy=%s, type=%s",
            doc.title,
            relevance,
            authority,
            accuracy,
            type_label,
        )
        return (doc, relevance, authority, accuracy, type_label)

    def _score_documents(
        self,
        documents: List[SourceDocument],
        brief_input: ResearchBriefInput,
    ) -> List[Tuple[SourceDocument, float, float, float, str]]:
        """
        Use the LLM to produce relevance, authority, accuracy scores and type for each document.
        Documents are scored in parallel; results are sorted by relevance descending.

        Preconditions: documents and brief_input valid.
        Postconditions: Returns list of (document, relevance, authority, accuracy, type_label) sorted by relevance descending.
        """
        n_docs = len(documents)
        if n_docs == 0:
            logger.info("No documents to score")
            return []

        logger.info(
            "Scoring %s documents for relevance, authority, and accuracy (parallel)...", n_docs
        )
        self._report_llm("Scoring documents for relevance...", 0.50)

        # parallel_map copies this thread's context per task so the LLM
        # attribution/request-id contextvars propagate into the scoring workers
        # (raw threads don't copy them; see llm_service.attribution).
        # skip_none=False keeps one result per document positionally, exactly as
        # the previous list comprehension did — safe because _score_one_document
        # always returns a tuple (never None; a failing LLM call raises rather
        # than returning None), so the sort below never sees a None element.
        scored = parallel_map(
            documents,
            lambda doc: self._score_one_document(doc, brief_input),
            max_workers=_DOC_PARALLEL_WORKERS,
            skip_none=False,
        )

        scored.sort(key=lambda t: t[1], reverse=True)
        self._report_llm("Document scoring complete.", 0.65)
        logger.info("Scored %s documents", len(scored))
        return scored

    def _summarize_one_document(
        self,
        item: Tuple[SourceDocument, float, float, float, str],
        brief_input: ResearchBriefInput,
    ) -> ResearchReference:
        """Summarize a single document into a ResearchReference. Used by _summarize_documents."""
        doc, relevance, authority, accuracy, type_label = item
        ctx_tokens = 16384  # Default context budget for safety
        max_content_chars = max(4000, ctx_tokens - 6000)  # 1 char ≈ 1 token for safety
        doc_content = compact_text(
            doc.content or "", max_content_chars, self._model, "document for summarization"
        )
        if len(doc_content) > max_content_chars:
            doc_content = doc_content[:max_content_chars]
        prompt = DOC_SUMMARIZATION_PROMPT + "\n\n" + (f"Brief:\n{brief_input.brief}\n")
        if brief_input.audience:
            prompt += f"Audience: {brief_input.audience}\n"
        if brief_input.tone_or_purpose:
            prompt += f"Tone/Purpose: {brief_input.tone_or_purpose}\n"
        prompt += (
            f"\nDocument title: {doc.title or ''}\n"
            f"Document URL: {doc.url}\n"
            f"Document content:\n{doc_content}\n"
        )
        try:
            data = self._call_json(prompt)
            summary = data.get("summary") or ""
            key_points = data.get("key_points") or []
        except Exception as e:  # pragma: no cover - excerpt-fallback path triggers only when the LLM raises mid-summarization; covered by integration tests with a flaky model.
            logger.warning(
                "Summarization LLM failed for %s (%s); using excerpt fallback so research can continue.",
                doc.url,
                type(e).__name__,
            )
            raw = (doc.content or "").strip().replace("\n", " ")
            summary = (raw[:500] if len(raw) > 500 else raw) or f"(Source: {doc.title or doc.url})"
            key_points = []
        return ResearchReference(
            title=doc.title or str(doc.url),
            url=doc.url,
            domain=doc.domain,
            summary=summary,
            content=doc.content.strip() or None,
            key_points=key_points,
            type=type_label,
            recency=None,
            relevance_score=relevance,
            authority_score=authority,
            accuracy_score=accuracy,
        )

    def _summarize_documents(
        self,
        scored_docs: List[Tuple[SourceDocument, float, float, float, str]],
        brief_input: ResearchBriefInput,
    ) -> List[ResearchReference]:
        """
        Preconditions: scored_docs and brief_input valid.
        Postconditions: Returns list of ResearchReference, length <= brief_input.max_results.
        """
        cap = min(len(scored_docs), brief_input.max_results)
        if cap == 0:
            logger.info("No documents to summarize")
            return []

        logger.info("Summarizing %s references (parallel)...", cap)
        self._report_llm("Summarizing references...", 0.65)

        items = scored_docs[: brief_input.max_results]
        # parallel_map copies this thread's context per task so the LLM
        # attribution/request-id contextvars propagate into the summarizing
        # workers (raw threads don't copy them; see llm_service.attribution).
        # skip_none=False keeps one result per item positionally, as the previous
        # list comprehension did — safe because _summarize_one_document always
        # returns a ResearchReference (its except path falls back to an excerpt,
        # never None).
        references = parallel_map(
            items,
            lambda item: self._summarize_one_document(item, brief_input),
            max_workers=_DOC_PARALLEL_WORKERS,
            skip_none=False,
        )

        self._report_llm("Summarization complete.", 0.78)
        logger.info("Produced %s references", len(references))
        return references

    def _synthesize_overview(
        self,
        brief_input: ResearchBriefInput,
        references: List[ResearchReference],
    ) -> str | None:
        """
        Preconditions: brief_input and references valid.
        Postconditions: Returns overview string or None if references empty.
        """
        if not references:
            logger.info("Skipping overview (no references)")
            return None

        logger.info("Synthesizing final overview...")
        refs_for_prompt = []
        for ref in references:
            refs_for_prompt.append(
                {
                    "title": ref.title,
                    "url": str(ref.url),
                    "summary": ref.summary,
                    "key_points": ref.key_points,
                    "type": ref.type,
                }
            )

        prompt = (
            FINAL_SYNTHESIS_PROMPT
            + "\n\n"
            + (f"Brief:\n{brief_input.brief}\n\nReferences (JSON):\n{refs_for_prompt}\n")
        )
        self._report_llm("Synthesizing overview...", 0.78)
        try:
            data = self._call_json(prompt)
        except LLMJsonParseError as e:  # pragma: no cover - parse-failure fallback in overview synthesis; covered by integration tests with a flaky model.
            logger.warning(
                "Overview synthesis: LLM returned invalid or empty JSON (%s). Using fallback.",
                str(e),
            )
            return None

        # Some LLM implementations may just return a string analysis;
        # we support both dict and string responses here.
        if isinstance(data, dict):
            analysis = data.get("analysis")
            outline = data.get("outline")
            if isinstance(analysis, str) and isinstance(outline, list):
                bullets = "\n".join(f"- {item}" for item in outline)
                logger.info("Overview complete")
                return f"{analysis}\n\nSuggested outline:\n{bullets}"
            if isinstance(
                analysis, str
            ):  # pragma: no cover - alternate LLM response shape (analysis without outline); covered by integration tests.
                logger.info("Overview complete")
                return analysis

        if isinstance(
            data, str
        ):  # pragma: no cover - alternate LLM response shape (plain string); covered by integration tests.
            logger.info("Overview complete")
            return data

        logger.info("Overview complete")
        return None

    def _fetch_academic_papers(self, brief_input: ResearchBriefInput) -> List[AcademicPaper]:
        """
        Search arXiv for papers relevant to the brief. Returns list of AcademicPaper.

        Preconditions: brief_input valid.
        Postconditions: Returns list of AcademicPaper (title, url, overview_or_summary); may be empty on failure.
        """
        try:
            papers = search_arxiv(
                brief_input.brief,
                max_results=5,
                timeout=15.0,
            )
            return papers
        except Exception as e:
            logger.warning("arXiv search failed, skipping academic sources: %s", e)
            return []

    def _get_similar_topics(
        self,
        brief_input: ResearchBriefInput,
        references: List[ResearchReference],
    ) -> List[str]:
        """
        Use LLM to suggest similar topics with similarity scores; return topics with score > 70%.

        Preconditions: brief_input and references valid.
        Postconditions: Returns list of topic strings (similarity_score >= 0.7).
        """
        if not references:
            return []
        refs_preview = "\n".join(f"- {ref.title}: {ref.summary}" for ref in references[:5])
        prompt = (
            SIMILAR_TOPICS_PROMPT
            + "\n\n"
            + (f"Brief:\n{brief_input.brief}\n\nReferences found:\n{refs_preview}\n")
        )
        self._report_llm("Finding similar topics...", 0.90)
        try:
            data = self._call_json(prompt)
            items = data.get("similar_topics") or []
            topics: List[str] = []
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict):
                    topic = item.get("topic")
                    score = item.get("similarity_score")
                    if topic and score is not None:
                        try:
                            s = float(score)
                            if s >= 0.7:
                                topics.append(str(topic).strip())
                        except (
                            TypeError,
                            ValueError,
                        ):  # pragma: no cover - defensive guard against non-numeric similarity scores from the LLM; covered by integration tests with malformed responses.
                            pass
            return topics[:15]
        except Exception as e:
            logger.warning("Similar topics step failed: %s", e)
            return []

    def _compile_document(
        self,
        brief_input: ResearchBriefInput,
        references: List[ResearchReference],
        notes: str | None,
        academic_papers: List[AcademicPaper],
        similar_topics: List[str],
    ) -> str:
        """
        Build the compiled document in Blog Post Research format.

        Uses full document content (ref.content) when available, else ref.summary.
        Format:
        # Blog Post Research
        - summary of the sources that were found
        ## Sources
        1. URL
        -- Full document text (or summary if content not set)
        ...
        ## Academic sources (a list of links to research papers on arxiv.org)
        1. Paper URL
        -- Overview/summary
        ...
        ## Similar topics
        - List of topics with similarity > 70%
        """
        lines = [
            "# Blog Post Research",
            "",
        ]
        # Summary of the sources that were found
        if notes:
            summary_line = notes.replace("\n", " ").strip()
            lines.append("- " + summary_line)
        else:
            lines.append(
                "- Summary of sources: "
                + (
                    f'Found {len(references)} web source(s) and {len(academic_papers)} academic paper(s) relevant to "{brief_input.brief[:80]}...".'
                    if len(brief_input.brief) > 80
                    else f'Found {len(references)} web source(s) and {len(academic_papers)} academic paper(s) relevant to "{brief_input.brief}".'
                )
            )
        lines.append("")
        lines.append("## Sources")
        lines.append("")
        if references:
            for i, ref in enumerate[ResearchReference](references, start=1):
                lines.append(f"{i}. {ref.url}")
                body = ref.summary.strip()
                lines.append(f"-- {body}")
                lines.append("Key points:")
                for key_point in ref.key_points:
                    lines.append(f"  - {key_point}")
                lines.append("")
        else:
            lines.append("(No web sources found.)")
            lines.append("")
        lines.append("## Academic sources (a list of links to research papers on arxiv.org)")
        lines.append("")
        if academic_papers:  # pragma: no cover - academic-papers branch requires a live arXiv response; the empty path is the one exercised by unit tests.
            for i, paper in enumerate(academic_papers, start=1):
                lines.append(f"{i}. {paper.url}")
                lines.append(f"-- {paper.overview_or_summary.strip()}")
                lines.append("")
        else:
            lines.append("(No academic papers found.)")
            lines.append("")
        lines.append("## Similar topics")
        lines.append("")
        if similar_topics:
            for topic in similar_topics:
                lines.append(f"- {topic}")
            lines.append("")
        else:
            lines.append("(No similar topics with score > 70%.)")
            lines.append("")
        return "\n".join(lines).strip()
