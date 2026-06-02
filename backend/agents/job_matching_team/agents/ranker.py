"""Score and rank discovered postings against the job-seeker profile."""

from __future__ import annotations

import logging
from typing import List, Optional

from ..models import JobPosting, RankedJob, SubScores
from ..profile.model import WEIGHT_FIELDS, JobSeekerProfile
from ..prompts import RANKER_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# Score bands used as a fallback recommendation when the LLM omits one.
_APPLY_THRESHOLD = 0.75
_MAYBE_THRESHOLD = 0.5


class JobRankerAgent:
    """Compute a weighted fit score and recommendation for each posting.

    Invariants:
        * :meth:`rank` returns postings sorted by descending ``score``.
        * Any posting hitting a hard exclusion (excluded company, below the
          salary floor with a stated salary, or a deal-breaker term) is forced
          to ``recommendation="skip"`` regardless of its numeric score.
    """

    def __init__(self, llm_client: Optional[object] = None) -> None:
        self._llm = llm_client

    def _client(self):  # noqa: ANN202
        if self._llm is None:
            from llm_service import get_client

            self._llm = get_client(agent_key="job_matching.ranker")
        return self._llm

    def rank(self, postings: List[JobPosting], profile: JobSeekerProfile) -> List[RankedJob]:
        """Score every posting and return them sorted best-first.

        Preconditions:
            * ``postings`` is a list of :class:`JobPosting` (may be empty).
        Postconditions:
            * Output length equals ``len(postings)``; ordering is by descending
              ``score`` (stable for ties).
        """
        weights = profile.weights.normalized()
        ranked = [self._score_one(p, profile, weights) for p in postings]
        ranked.sort(key=lambda r: r.score, reverse=True)
        return ranked

    def _score_one(
        self, posting: JobPosting, profile: JobSeekerProfile, weights: dict[str, float]
    ) -> RankedJob:
        sub, recommendation, rationale, concerns = self._judge(posting, profile)
        score = sum(weights[f] * getattr(sub, f) for f in WEIGHT_FIELDS)
        score = max(0.0, min(1.0, score))

        hard_skip, skip_reasons = self._hard_exclusions(posting, profile)
        if hard_skip:
            recommendation = "skip"
            concerns = list(dict.fromkeys([*concerns, *skip_reasons]))
        elif recommendation not in ("apply", "maybe", "skip"):
            recommendation = self._band(score)

        return RankedJob(
            posting=posting,
            score=round(score, 4),
            sub_scores=sub,
            recommendation=recommendation,  # type: ignore[arg-type]
            rationale=rationale,
            concerns=concerns,
        )

    def _judge(
        self, posting: JobPosting, profile: JobSeekerProfile
    ) -> tuple[SubScores, str, str, List[str]]:
        """Ask the LLM for per-dimension sub-scores; default to neutral on failure."""
        try:
            prompt = (
                f"Job seeker criteria (JSON):\n{profile.model_dump_json(indent=2)}\n\n"
                f"Job posting (JSON):\n{posting.model_dump_json(indent=2)}"
            )
            data = self._client().complete_json(
                prompt,
                temperature=0.1,
                system_prompt=RANKER_SYSTEM_PROMPT,
            )
        except Exception:  # noqa: BLE001 - scoring is best-effort per posting
            logger.warning("Ranker LLM call failed for %s", posting.url, exc_info=True)
            data = {}

        if not isinstance(data, dict):
            data = {}

        sub = SubScores(
            title_fit=_clamp(data.get("title_fit")),
            seniority_fit=_clamp(data.get("seniority_fit")),
            location_fit=_clamp(data.get("location_fit")),
            comp_fit=_clamp(data.get("comp_fit"), default=0.5),
            company_fit=_clamp(data.get("company_fit")),
            skills_fit=_clamp(data.get("skills_fit")),
        )
        recommendation = str(data.get("recommendation") or "").lower().strip()
        rationale = str(data.get("rationale") or "").strip()
        concerns_raw = data.get("concerns") or []
        concerns = (
            [str(c).strip() for c in concerns_raw if str(c).strip()]
            if isinstance(concerns_raw, list)
            else []
        )
        return sub, recommendation, rationale, concerns

    def _hard_exclusions(
        self, posting: JobPosting, profile: JobSeekerProfile
    ) -> tuple[bool, List[str]]:
        """Return (should_skip, reasons) from deterministic exclusion rules."""
        reasons: List[str] = []
        company_l = posting.company.lower()
        for excluded in profile.excluded_companies:
            if excluded and excluded.lower() in company_l:
                reasons.append(f"Excluded company: {posting.company}")
                break

        if (
            profile.salary_min
            and posting.salary_max is not None
            and posting.salary_max < profile.salary_min
        ):
            reasons.append(
                f"Stated max {posting.salary_max} below floor {profile.salary_min} {profile.currency}"
            )

        haystack = f"{posting.title}\n{posting.description}".lower()
        for term in profile.deal_breakers:
            if term and term.lower() in haystack:
                reasons.append(f"Deal-breaker present: {term}")
        return (bool(reasons), reasons)

    @staticmethod
    def _band(score: float) -> str:
        if score >= _APPLY_THRESHOLD:
            return "apply"
        if score >= _MAYBE_THRESHOLD:
            return "maybe"
        return "skip"


def _clamp(value: object, *, default: float = 0.0) -> float:
    """Coerce ``value`` into the ``[0, 1]`` range, using ``default`` on failure."""
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, f))
