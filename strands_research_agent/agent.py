from __future__ import annotations

from typing import List, Tuple

from pydantic import HttpUrl

from .llm import LLMClient
from .models import (
    ResearchBriefInput,
    ResearchAgentOutput,
    ResearchReference,
    SearchQuery,
    CandidateResult,
    SourceDocument,
)
from .prompts import (
    BRIEF_PARSING_PROMPT,
    QUERY_GENERATION_PROMPT,
    DOC_RELEVANCE_SCORING_PROMPT,
    DOC_SUMMARIZATION_PROMPT,
    FINAL_SYNTHESIS_PROMPT,
)
from .tools.web_search import TavilyWebSearch
from .tools.web_fetch import SimpleWebFetcher


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
    ) -> None:
        self.llm = llm_client
        self.web_search = web_search or TavilyWebSearch()
        self.web_fetcher = web_fetcher or SimpleWebFetcher()
        self.max_fetch_documents = max_fetch_documents

    # Public API ---------------------------------------------------------

    def run(self, brief_input: ResearchBriefInput) -> ResearchAgentOutput:
        """Execute the full research workflow and return structured output."""
        normalized = self._parse_brief(brief_input)
        queries = self._generate_queries(brief_input, normalized)
        candidates = self._run_searches(queries, brief_input)
        documents = self._fetch_documents(candidates, brief_input)
        scored_docs = self._score_documents(documents, brief_input)
        references = self._summarize_documents(scored_docs, brief_input)
        notes = self._synthesize_overview(brief_input, references)

        return ResearchAgentOutput(query_plan=queries, references=references, notes=notes)

    # Steps --------------------------------------------------------------

    def _parse_brief(self, brief_input: ResearchBriefInput) -> dict:
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

        return queries

    def _run_searches(
        self,
        queries: List[SearchQuery],
        brief_input: ResearchBriefInput,
    ) -> List[CandidateResult]:
        seen_urls = set()
        candidates: List[CandidateResult] = []

        for query in queries:
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

        return candidates

    def _fetch_documents(
        self,
        candidates: List[CandidateResult],
        brief_input: ResearchBriefInput,
    ) -> List[SourceDocument]:
        documents: List[SourceDocument] = []
        # Cap total documents to avoid excessive latency/cost.
        max_docs = min(self.max_fetch_documents, len(candidates))

        for candidate in candidates[:max_docs]:
            try:
                doc = self.web_fetcher.fetch(HttpUrl(str(candidate.url)))
            except Exception:
                # Best-effort: skip failures.
                continue
            documents.append(doc)

        return documents

    def _score_documents(
        self,
        documents: List[SourceDocument],
        brief_input: ResearchBriefInput,
    ) -> List[Tuple[SourceDocument, float, str]]:
        """
        Use the LLM to produce a relevance score and type for each document.

        Returns a list of (document, relevance_score, type_label) sorted by score.
        """
        scored: List[Tuple[SourceDocument, float, str]] = []

        for doc in documents:
            excerpt = doc.content[:4000]
            prompt = DOC_RELEVANCE_SCORING_PROMPT + "\n\n" + (
                f"Brief:\n{brief_input.brief}\n\n"
                f"Document title: {doc.title or ''}\n"
                f"Document excerpt:\n{excerpt}\n"
            )
            data = self.llm.complete_json(prompt, temperature=0.0)
            score = data.get("relevance_score")
            if not isinstance(score, (int, float)):
                score = 0.0
            type_label = data.get("type") or None
            scored.append((doc, float(score), type_label))

        # Sort by relevance descending.
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored

    def _summarize_documents(
        self,
        scored_docs: List[Tuple[SourceDocument, float, str]],
        brief_input: ResearchBriefInput,
    ) -> List[ResearchReference]:
        references: List[ResearchReference] = []

        for doc, score, type_label in scored_docs[: brief_input.max_results]:
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
                    relevance_score=float(score),
                )
            )

        return references

    def _synthesize_overview(
        self,
        brief_input: ResearchBriefInput,
        references: List[ResearchReference],
    ) -> str | None:
        if not references:
            return None

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
        data = self.llm.complete_json(prompt, temperature=0.3)

        # Some LLM implementations may just return a string analysis;
        # we support both dict and string responses here.
        if isinstance(data, dict):
            analysis = data.get("analysis")
            outline = data.get("outline")
            if isinstance(analysis, str) and isinstance(outline, list):
                bullets = "\n".join(f"- {item}" for item in outline)
                return f"{analysis}\n\nSuggested outline:\n{bullets}"
            if isinstance(analysis, str):
                return analysis

        if isinstance(data, str):
            return data

        return None

