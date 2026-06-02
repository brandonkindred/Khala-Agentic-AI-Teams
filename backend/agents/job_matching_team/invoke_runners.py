"""Invoke-shim entrypoints for job matching agents.

Each function accepts a raw ``body`` dict from the Agent Console sandbox
dispatcher and delegates to the corresponding agent. The dispatcher calls
plain functions directly (no factory or class instantiation needed).
"""

from __future__ import annotations

from typing import Any

from .agents.ranker import JobRankerAgent
from .agents.scanner import JobScannerAgent
from .models import JobPosting
from .profile.model import JobSeekerProfile


def invoke_scanner(body: dict[str, Any]) -> dict[str, Any]:
    """Run the scanner over caller-supplied queries.

    Body shape: ``{"queries": [str, ...], "max_roles": int}``.
    Requires live web access (``OLLAMA_API_KEY``).
    """
    queries = [str(q) for q in (body.get("queries") or []) if str(q).strip()]
    if not queries:
        raise ValueError("invoke_scanner requires a non-empty 'queries' list")
    max_roles = int(body.get("max_roles", 20))
    postings = JobScannerAgent().scan(queries, max_roles=max_roles)
    return {"postings": [p.model_dump(mode="json") for p in postings]}


def invoke_ranker(body: dict[str, Any]) -> dict[str, Any]:
    """Rank caller-supplied postings against a profile.

    Body shape: ``{"profile": {...}, "postings": [{...}, ...]}``.
    Pure LLM scoring — no live web access required.
    """
    profile = JobSeekerProfile.model_validate(body.get("profile") or {})
    postings = [
        JobPosting.model_validate(p).ensure_fingerprint() for p in (body.get("postings") or [])
    ]
    ranked = JobRankerAgent().rank(postings, profile)
    return {"ranked_jobs": [rj.model_dump(mode="json") for rj in ranked]}
