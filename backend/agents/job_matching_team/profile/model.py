"""Pydantic model for a job-seeker profile.

Kept intentionally permissive: every field has a sensible default so a
partial profile still validates. The profile holds the user's *standing*
search criteria; individual scan requests may override any field.

Invariants:
    * ``RankingWeights`` always exposes six non-negative component weights.
      ``normalized()`` returns weights that sum to 1.0 (falling back to a
      uniform 1/6 split when every weight is zero), so the ranker's weighted
      sum is always a convex combination of the per-criterion sub-scores.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Literal

from pydantic import BaseModel, Field

RemotePreference = Literal["remote", "hybrid", "onsite", "any"]

# The six scoring dimensions the ranker blends into a single 0..1 score.
WEIGHT_FIELDS = (
    "title_fit",
    "seniority_fit",
    "location_fit",
    "comp_fit",
    "company_fit",
    "skills_fit",
)


class RankingWeights(BaseModel):
    """Relative importance of each scoring dimension (need not sum to 1.0).

    Invariants:
        * Every component is ``>= 0`` (enforced by ``ge=0``).
    """

    title_fit: float = Field(default=0.25, ge=0)
    seniority_fit: float = Field(default=0.10, ge=0)
    location_fit: float = Field(default=0.15, ge=0)
    comp_fit: float = Field(default=0.15, ge=0)
    company_fit: float = Field(default=0.15, ge=0)
    skills_fit: float = Field(default=0.20, ge=0)

    def normalized(self) -> dict[str, float]:
        """Return component weights scaled to sum to 1.0.

        Postconditions:
            * The returned dict has exactly the keys in ``WEIGHT_FIELDS``.
            * Values are non-negative and sum to 1.0 (within float error).
            * When all weights are zero, falls back to a uniform split so the
              ranker never divides by zero.
        """
        raw = {f: float(getattr(self, f)) for f in WEIGHT_FIELDS}
        total = sum(raw.values())
        if total <= 0:
            uniform = 1.0 / len(WEIGHT_FIELDS)
            return {f: uniform for f in WEIGHT_FIELDS}
        return {f: v / total for f, v in raw.items()}


class JobSeekerProfile(BaseModel):
    """The user's standing job-search criteria, injected into the pipeline."""

    target_titles: List[str] = Field(default_factory=list)
    seniority_levels: List[str] = Field(default_factory=list)
    locations: List[str] = Field(default_factory=list)
    remote_preference: RemotePreference = "any"
    salary_min: int = 0
    currency: str = "USD"
    company_stages: List[str] = Field(default_factory=list)
    company_sizes: List[str] = Field(default_factory=list)
    industries: List[str] = Field(default_factory=list)
    must_have_skills: List[str] = Field(default_factory=list)
    nice_to_have_skills: List[str] = Field(default_factory=list)
    deal_breakers: List[str] = Field(default_factory=list)
    preferred_companies: List[str] = Field(default_factory=list)
    excluded_companies: List[str] = Field(default_factory=list)
    work_authorization: str = ""
    keywords: List[str] = Field(default_factory=list)
    weights: RankingWeights = Field(default_factory=RankingWeights)

    @classmethod
    def from_yaml_file(cls, path: str | Path) -> "JobSeekerProfile":
        """Load and validate a profile from a YAML file.

        Preconditions:
            * ``path`` points to a readable YAML file (caller-checked).
        Postconditions:
            * Returns a fully-defaulted ``JobSeekerProfile``; an empty file
              yields an all-defaults profile rather than raising.
        """
        import yaml

        text = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
        return cls.model_validate(data)

    def merged_with(self, overrides: dict | None) -> "JobSeekerProfile":
        """Return a copy with non-null ``overrides`` applied on top.

        Preconditions:
            * ``overrides`` is ``None`` or a dict whose keys are a subset of
              this model's fields. Unknown keys are ignored.
        Postconditions:
            * Returns a new validated profile; ``self`` is not mutated.
            * Keys mapping to ``None`` are ignored (the standing value wins).
            * A partial ``weights`` override is merged onto the standing
              weights, so omitted weight components keep their configured
              values instead of resetting to class defaults.
        """
        if not overrides:
            return self.model_copy(deep=True)
        valid = {
            k: v
            for k, v in overrides.items()
            if k in JobSeekerProfile.model_fields and v is not None
        }
        base = self.model_dump()
        # Deep-merge the nested ``weights`` mapping so a caller can tune a
        # single component without discarding the others (a shallow update
        # would replace the whole sub-object).
        weights_override = valid.get("weights")
        if isinstance(weights_override, dict):
            merged_weights = {**base.get("weights", {}), **weights_override}
            valid = {**valid, "weights": merged_weights}
        base.update(valid)
        return JobSeekerProfile.model_validate(base)
