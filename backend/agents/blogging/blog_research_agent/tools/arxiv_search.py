"""
Search arXiv for research papers relevant to a brief.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import List
from xml.etree import ElementTree

import httpx

from ..models import AcademicPaper

logger = logging.getLogger(__name__)

ARXiv_API = "https://export.arxiv.org/api/query"
NS = {"atom": "http://www.w3.org/2005/Atom"}


class ArxivSearchError(RuntimeError):
    """Raised when arXiv search fails."""


def search_arxiv(
    query: str,
    *,
    max_results: int = 5,
    timeout: float = 15.0,
) -> List[AcademicPaper]:
    """
    Search arXiv for papers matching the query. Returns papers with title, URL, and abstract.

    Preconditions:
        - query is a non-empty string.
        - max_results >= 1.
    Postconditions:
        - Returns a list of AcademicPaper (title, url, overview_or_summary from abstract).
        - Raises ArxivSearchError on API or network failure.
    """
    if not query or not query.strip():
        return []
    if max_results < 1:
        max_results = 1

    # Build search_query: search in title and abstract
    search_query = f"all:{query.strip()}"
    params = {
        "search_query": search_query,
        "start": 0,
        "max_results": max_results,
    }
    url = f"{ARXiv_API}?{urllib.parse.urlencode(params)}"

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        raise ArxivSearchError(f"arXiv request failed: {exc}") from exc

    if resp.status_code >= 400:
        raise ArxivSearchError(f"arXiv API returned {resp.status_code}: {resp.text[:200]}")

    papers: List[AcademicPaper] = []
    try:
        root = ElementTree.fromstring(resp.content)
        for entry in root.findall("atom:entry", NS):
            title_el = entry.find("atom:title", NS)
            id_el = entry.find("atom:id", NS)
            summary_el = entry.find("atom:summary", NS)
            title = (title_el.text or "").strip() if title_el is not None else ""
            # arXiv returns id like https://arxiv.org/abs/1234.5678
            link = (id_el.text or "").strip() if id_el is not None else ""
            abstract = (summary_el.text or "").strip() if summary_el is not None else ""
            # Normalize whitespace in abstract
            abstract = re.sub(r"\s+", " ", abstract).strip() if abstract else ""
            if not link:
                continue
            try:
                papers.append(
                    AcademicPaper(
                        title=title or "Untitled",
                        url=link,
                        overview_or_summary=abstract or "No abstract available.",
                    )
                )
            except Exception as e:
                logger.debug("Skip invalid arXiv entry: %s", e)
                continue
    except ElementTree.ParseError as e:
        raise ArxivSearchError(f"Failed to parse arXiv response: {e}") from e

    logger.info("arXiv search returned %s papers for query=%s", len(papers), query[:50])
    return papers
