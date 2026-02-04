from __future__ import annotations

import ast
import logging
from typing import List, Tuple

from pydantic import HttpUrl

logger = logging.getLogger(__name__)

from .llm import LLMClient
from .models import (
    ResearchBriefInput,
    ResearchAgentOutput,
    ResearchReference,
    SearchQuery,
    CandidateResult,
    SourceDocument,
    AcademicPaper,
)
from .prompts import (
    BRIEF_PARSING_PROMPT,
    QUERY_GENERATION_PROMPT,
    DOC_RELEVANCE_SCORING_PROMPT,
    DOC_SUMMARIZATION_PROMPT,
    FINAL_SYNTHESIS_PROMPT,
    SIMILAR_TOPICS_PROMPT,
)
from .tools.web_search import TavilyWebSearch
from .tools.web_fetch import SimpleWebFetcher
from .tools.arxiv_search import search_arxiv
from .agent_cache import AgentCache


class ResearchAgent:
    """
    Core research agent implementing the workflow defined in the plan.

    It is intentionally stateless beyond the constructor dependencies so
    it can be easily embedded in a Strands runtime or other orchestrator.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        web_search: TavilyWebSearch | None = None,
        web_fetcher: SimpleWebFetcher | None = None,
        max_fetch_documents: int = 20,
        cache: AgentCache | None = None,
    ) -> None:
        """
        Preconditions:
            - llm_client is not None.
            - max_fetch_documents >= 1.
        Invariants (after construction):
            - self.llm is not None.
            - self.max_fetch_documents >= 1.
        """
        assert llm_client is not None, "llm_client is required"
        assert max_fetch_documents >= 1, "max_fetch_documents must be at least 1"
        self.llm = llm_client
        self.web_search = web_search or TavilyWebSearch()
        self.web_fetcher = web_fetcher or SimpleWebFetcher()
        self.max_fetch_documents = max_fetch_documents
        self.cache = cache

    # Public API ---------------------------------------------------------

    def run(self, brief_input: ResearchBriefInput) -> ResearchAgentOutput:
        """
        Execute the full research workflow and return structured output.

        If cache is enabled, will resume from the last completed step on failure.

        Preconditions:
            - brief_input is a valid ResearchBriefInput (e.g. from model_validate).
        Postconditions:
            - Returns ResearchAgentOutput with query_plan (list), references (list,
              length <= brief_input.max_results), notes (str or None), and
              compiled_document (formatted document of most relevant links with summaries).
        """
        brief_preview = (
            (brief_input.brief[:77] + "...") if len(brief_input.brief) > 80 else brief_input.brief
        )
        logger.info(
            "Starting research: brief=%s, max_results=%s",
            brief_preview,
            brief_input.max_results,
        )

        # Try to load checkpoint
        cached_state = None
        if self.cache:
            cached_state = self.cache.load_checkpoint(brief_input)
            if cached_state:
                logger.info("Resuming from checkpoint: last_step=%s", cached_state.last_completed_step)

        # Step 1: Parse brief
        if cached_state and cached_state.normalized:
            logger.info("Using cached normalized brief")
            normalized = cached_state.normalized
        else:
            normalized = self._parse_brief(brief_input)
            if self.cache:
                self.cache.save_checkpoint(brief_input, "normalized", normalized=normalized)

        # Step 2: Generate queries
        if cached_state and cached_state.queries:
            logger.info("Using cached queries (%s)", len(cached_state.queries))
            queries = [SearchQuery(**q) for q in cached_state.queries]
        else:
            queries = self._generate_queries(brief_input, normalized)
            if self.cache:
                self.cache.save_checkpoint(brief_input, "queries", queries=queries)

        # Step 3: Run searches
        if cached_state and cached_state.candidates:
            logger.info("Using cached candidates (%s)", len(cached_state.candidates))
            candidates = [CandidateResult(**c) for c in cached_state.candidates]
        else:
            candidates = self._run_searches(queries, brief_input)
            if self.cache:
                self.cache.save_checkpoint(brief_input, "candidates", candidates=candidates)

        # Step 4: Fetch documents
        if cached_state and cached_state.documents:
            logger.info("Using cached documents (%s)", len(cached_state.documents))
            documents = [SourceDocument(**d) for d in cached_state.documents]
        else:
            documents = self._fetch_documents(candidates, brief_input)
            if self.cache:
                self.cache.save_checkpoint(brief_input, "documents", documents=documents)

        # Step 5: Score documents
        if cached_state and cached_state.scored_docs:
            logger.info("Using cached scored documents (%s)", len(cached_state.scored_docs))
            scored_docs = []
            for item in cached_state.scored_docs:
                # Support old format [doc, score, type] and new [doc, relevance, authority, accuracy, type]
                if len(item) >= 5:
                    scored_docs.append((
                        SourceDocument(**item[0]), item[1], item[2], item[3], item[4]
                    ))
                else:
                    scored_docs.append((
                        SourceDocument(**item[0]), item[1], 0.5, 0.5, item[2] if len(item) > 2 else None
                    ))
        else:
            scored_docs = self._score_documents(documents, brief_input)
            if self.cache:
                self.cache.save_checkpoint(brief_input, "scored_docs", scored_docs=scored_docs)

        # Step 6: Summarize documents
        if cached_state and cached_state.references:
            logger.info("Using cached references (%s)", len(cached_state.references))
            references = [ResearchReference(**r) for r in cached_state.references]
        else:
            references = self._summarize_documents(scored_docs, brief_input)
            if self.cache:
                self.cache.save_checkpoint(brief_input, "references", references=references)

        # Step 7: Synthesize overview
        if cached_state and cached_state.notes is not None:
            logger.info("Using cached notes")
            notes = cached_state.notes
        else:
            notes = self._synthesize_overview(brief_input, references)
            if self.cache:
                self.cache.save_checkpoint(brief_input, "notes", notes=notes)

        # Step 8: Fetch academic sources (arXiv)
        academic_papers = self._fetch_academic_papers(brief_input)

        # Step 9: Similar topics (score > 70%)
        similar_topics = self._get_similar_topics(brief_input, references)

        # Step 10: Compile document (Blog Post Research format)
        compiled_document = self._compile_document(
            brief_input, references, notes, academic_papers, similar_topics
        )

        logger.info(
            "Research complete: %s references, %s academic papers, %s similar topics, compiled_document=%s",
            len(references),
            len(academic_papers),
            len(similar_topics),
            len(compiled_document) if compiled_document else 0,
        )
        return ResearchAgentOutput(
            query_plan=queries,
            references=references,
            notes=notes,
            compiled_document=compiled_document,
            academic_papers=academic_papers,
            similar_topics=similar_topics,
        )

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

        parsed = self.llm.complete_json(prompt, temperature=0.0)

        return {
            "core_topics": parsed.get("core_topics") or [brief_input.brief],
            "angle": parsed.get("angle") or "",
            "constraints": parsed.get("constraints") or [],
        }

    def _generate_queries(self, brief_input: ResearchBriefInput, normalized: dict) -> List[SearchQuery]:
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
        data = self.llm.complete_json(prompt, temperature=0.3)
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

    def _score_documents(
        self,
        documents: List[SourceDocument],
        brief_input: ResearchBriefInput,
    ) -> List[Tuple[SourceDocument, float, float, float, str]]:
        """
        Use the LLM to produce relevance, authority, accuracy scores and type for each document.

        Preconditions: documents and brief_input valid.
        Postconditions: Returns list of (document, relevance, authority, accuracy, type_label) sorted by relevance descending.
        """
        logger.info("Scoring documents for relevance, authority, and accuracy...")
        scored: List[Tuple[SourceDocument, float, float, float, str]] = []

        for doc in documents:
            excerpt = doc.content[:4000]
            prompt = DOC_RELEVANCE_SCORING_PROMPT + "\n\n" + (
                f"Brief:\n{brief_input.brief}\n\n"
                f"Document title: {doc.title or ''}\n"
                f"Document excerpt:\n{excerpt}\n"
            )
            data = self.llm.complete_json(prompt, temperature=0.0)
            rel = data.get("relevance_score")
            auth = data.get("authority_score")
            acc = data.get("accuracy_score")
            if not isinstance(rel, (int, float)):
                rel = 0.0
            if not isinstance(auth, (int, float)):
                auth = 0.5
            if not isinstance(acc, (int, float)):
                acc = 0.5
            relevance = max(0.0, min(1.0, float(rel)))
            authority = max(0.0, min(1.0, float(auth)))
            accuracy = max(0.0, min(1.0, float(acc)))
            type_label = data.get("type") or None
            scored.append((doc, relevance, authority, accuracy, type_label))
            logger.debug(
                "Scored doc: title=%s, relevance=%s, authority=%s, accuracy=%s, type=%s",
                doc.title, relevance, authority, accuracy, type_label,
            )

        # Sort by relevance descending.
        scored.sort(key=lambda t: t[1], reverse=True)
        logger.info("Scored %s documents", len(scored))
        return scored

    def _summarize_documents(
        self,
        scored_docs: List[Tuple[SourceDocument, float, float, float, str]],
        brief_input: ResearchBriefInput,
    ) -> List[ResearchReference]:
        """
        Preconditions: scored_docs and brief_input valid.
        Postconditions: Returns list of ResearchReference, length <= brief_input.max_results.
        """
        logger.info("Summarizing references...")
        references: List[ResearchReference] = []
        cap = min(len(scored_docs), brief_input.max_results)

        for idx, (doc, relevance, authority, accuracy, type_label) in enumerate(scored_docs[: brief_input.max_results]):
            logger.debug("Summarizing reference %s/%s", idx + 1, cap)
            excerpt = doc.content[:8000]
            prompt = DOC_SUMMARIZATION_PROMPT + "\n\n" + (
                f"Brief:\n{brief_input.brief}\n"
            )
            if brief_input.audience:
                prompt += f"Audience: {brief_input.audience}\n"
            if brief_input.tone_or_purpose:
                prompt += f"Tone/Purpose: {brief_input.tone_or_purpose}\n"
            prompt += (
                f"\nDocument title: {doc.title or ''}\n"
                f"Document URL: {doc.url}\n"
                f"Document excerpt:\n{excerpt}\n"
            )

            data = self.llm.complete_json(prompt, temperature=0.2)
            summary = data.get("summary") or ""
            key_points = data.get("key_points") or []

            references.append(
                ResearchReference(
                    title=doc.title or str(doc.url),
                    url=doc.url,
                    domain=doc.domain,
                    summary=summary,
                    key_points=key_points,
                    type=type_label,
                    recency=None,  # Could be set from publish_date/metadata in the future
                    relevance_score=relevance,
                    authority_score=authority,
                    accuracy_score=accuracy,
                )
            )

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

        prompt = FINAL_SYNTHESIS_PROMPT + "\n\n" + (
            f"Brief:\n{brief_input.brief}\n\n"
            f"References (JSON):\n{refs_for_prompt}\n"
        )
        try:
            data = self.llm.complete_json(prompt, temperature=0.3)
        except ValueError as e:
            # LLM may return prose/markdown instead of JSON; treat as analysis
            msg = str(e)
            prefix = "Could not parse JSON from Ollama response: "
            if msg.startswith(prefix):
                try:
                    raw = ast.literal_eval(msg[len(prefix) :])
                    data = {"analysis": raw, "outline": []}
                except (ValueError, SyntaxError):
                    raise
            else:
                raise

        # Some LLM implementations may just return a string analysis;
        # we support both dict and string responses here.
        if isinstance(data, dict):
            analysis = data.get("analysis")
            outline = data.get("outline")
            if isinstance(analysis, str) and isinstance(outline, list):
                bullets = "\n".join(f"- {item}" for item in outline)
                logger.info("Overview complete")
                return f"{analysis}\n\nSuggested outline:\n{bullets}"
            if isinstance(analysis, str):
                logger.info("Overview complete")
                return analysis

        if isinstance(data, str):
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
        refs_preview = "\n".join(
            f"- {ref.title}: {ref.summary[:150]}..." if len(ref.summary) > 150 else f"- {ref.title}: {ref.summary}"
            for ref in references[:5]
        )
        prompt = SIMILAR_TOPICS_PROMPT + "\n\n" + (
            f"Brief:\n{brief_input.brief}\n\n"
            f"References found:\n{refs_preview}\n"
        )
        try:
            data = self.llm.complete_json(prompt, temperature=0.2)
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
                        except (TypeError, ValueError):
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

        Format:
        # Blog Post Research
        - summary of the sources that were found
        ## Sources
        1. URL
        -- Summary
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
            summary_line = notes.replace("\n", " ").strip()[:2000]
            lines.append("- " + summary_line)
        else:
            lines.append("- Summary of sources: " + (
                f"Found {len(references)} web source(s) and {len(academic_papers)} academic paper(s) relevant to \"{brief_input.brief[:80]}...\"." if len(brief_input.brief) > 80 else f"Found {len(references)} web source(s) and {len(academic_papers)} academic paper(s) relevant to \"{brief_input.brief}\"."
            ))
        lines.append("")
        lines.append("## Sources")
        lines.append("")
        if references:
            for i, ref in enumerate(references, start=1):
                lines.append(f"{i}. {ref.url}")
                lines.append(f"-- {ref.summary.strip()}")
                lines.append("")
        else:
            lines.append("(No web sources found.)")
            lines.append("")
        lines.append("## Academic sources (a list of links to research papers on arxiv.org)")
        lines.append("")
        if academic_papers:
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

