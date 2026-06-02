"""Request/response and domain models for the job matching team.

Domain flow: a scan request (with optional profile overrides) produces a
list of normalized :class:`JobPosting` objects, each scored into a
:class:`RankedJob`, returned as a sorted :class:`JobMatchResponse`.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from .profile.model import JobSeekerProfile

Recommendation = Literal["apply", "maybe", "skip"]
RemoteMode = Literal["remote", "hybrid", "onsite", "unknown"]


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def compute_fingerprint(company: str, title: str, location: str) -> str:
    """Return a stable de-duplication key for a posting.

    Preconditions:
        * ``company``, ``title``, ``location`` are strings (may be empty).
    Postconditions:
        * Returns a 16-char lowercase hex digest. Two postings with the same
          normalized (company, title, location) always produce the same key,
          regardless of whitespace/case/punctuation differences.
    """

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

    raw = f"{norm(company)}|{norm(title)}|{norm(location)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class JobPosting(BaseModel):
    """A single open role discovered and normalized from the web."""

    title: str = ""
    company: str = ""
    location: str = ""
    remote_mode: RemoteMode = "unknown"
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    currency: str = "USD"
    url: str = ""
    source: str = ""
    description: str = ""
    posted_at: Optional[str] = None
    fingerprint: str = ""

    def ensure_fingerprint(self) -> "JobPosting":
        """Populate ``fingerprint`` from (company, title, location) if unset.

        Postconditions:
            * ``self.fingerprint`` is a non-empty 16-char hex string.
            * Returns ``self`` for chaining.
        """
        if not self.fingerprint:
            self.fingerprint = compute_fingerprint(self.company, self.title, self.location)
        return self


class SubScores(BaseModel):
    """Per-dimension fit scores, each in ``[0, 1]``."""

    title_fit: float = Field(default=0.0, ge=0.0, le=1.0)
    seniority_fit: float = Field(default=0.0, ge=0.0, le=1.0)
    location_fit: float = Field(default=0.0, ge=0.0, le=1.0)
    comp_fit: float = Field(default=0.0, ge=0.0, le=1.0)
    company_fit: float = Field(default=0.0, ge=0.0, le=1.0)
    skills_fit: float = Field(default=0.0, ge=0.0, le=1.0)


class RankedJob(BaseModel):
    """A posting plus its computed score, recommendation, and rationale."""

    posting: JobPosting
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    sub_scores: SubScores = Field(default_factory=SubScores)
    recommendation: Recommendation = "maybe"
    rationale: str = ""
    concerns: List[str] = Field(default_factory=list)


class JobMatchRequest(BaseModel):
    """Parameters for a single scan-and-rank run.

    All criteria come from the stored profile; ``profile_overrides`` lets a
    caller adjust any profile field for this run only.
    """

    profile_overrides: Optional[dict] = Field(
        default=None,
        description="Per-run overrides applied on top of the stored job-seeker profile.",
    )
    max_queries: int = Field(
        default=6, ge=1, le=25, description="Max distinct search queries to issue."
    )
    max_roles: int = Field(
        default=40, ge=1, le=200, description="Max postings to collect before ranking."
    )
    top_n: int = Field(default=15, ge=1, le=200, description="Number of ranked roles to return.")
    exclude_seen: bool = Field(
        default=False,
        description="When true, skip postings whose fingerprint was ranked in a prior run.",
    )


class JobMatchResponse(BaseModel):
    """The ranked result of a scan run."""

    run_id: str
    ranked_jobs: List[RankedJob] = Field(default_factory=list)
    total_found: int = 0
    total_ranked: int = 0
    profile_snapshot: JobSeekerProfile
    generated_at: str = Field(default_factory=_now_iso)


# ---------------------------------------------------------------------------
# Async job DTOs (submit / poll)
# ---------------------------------------------------------------------------


class ScanJobResponse(BaseModel):
    job_id: str
    status: str


class ScanJobStatus(BaseModel):
    job_id: str
    status: str
    result: Optional[JobMatchResponse] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ScanJobListItem(BaseModel):
    job_id: str
    status: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ScanJobListResponse(BaseModel):
    jobs: List[ScanJobListItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Persistence read models
# ---------------------------------------------------------------------------


class RunSummary(BaseModel):
    run_id: str
    status: str
    total_found: int = 0
    total_ranked: int = 0
    created_at: Optional[str] = None
    completed_at: Optional[str] = None


class RunDetail(BaseModel):
    run_id: str
    status: str
    total_found: int = 0
    total_ranked: int = 0
    created_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    ranked_jobs: List[RankedJob] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Agent Console invoke schemas (single-agent, Runner tab)
# ---------------------------------------------------------------------------


class ScannerInvokeRequest(BaseModel):
    queries: List[str] = Field(..., min_length=1, description="Web search queries to scan.")
    max_roles: int = Field(default=20, ge=1, le=200)


class ScannerInvokeResponse(BaseModel):
    postings: List[JobPosting] = Field(default_factory=list)


class RankerInvokeRequest(BaseModel):
    profile: JobSeekerProfile
    postings: List[JobPosting] = Field(default_factory=list)


class RankerInvokeResponse(BaseModel):
    ranked_jobs: List[RankedJob] = Field(default_factory=list)
