"""Serializable payloads that cross the code-review Temporal activity boundaries.

Every value passed into or out of an activity crosses Temporal's default JSON
data converter, so the workflow and its activities exchange plain
``model_dump(mode="json")`` dicts and reconstruct them with ``model_validate``
(the repo registers no custom ``pydantic_data_converter``; the shared worker's
sandbox passthrough handles pydantic itself).

Two intermediate payloads are defined here:

- :class:`ReviewPrepDTO` — the output of the prepare activity: the compacted
  shared context, the bounded review chunks, and the fingerprints the map phase
  needs.
- :class:`ChunkOutcomeDTO` — the JSON-native mirror of the coordinator's private
  ``mapping._ChunkOutcome`` dataclass, so one chunk's result (findings, verdict,
  summaries) survives the map-activity return trip.

The existing :class:`~code_review_agent.models.CodeReviewInput` /
:class:`~code_review_agent.models.CodeReviewOutput` /
:class:`~code_review_agent.models.CodeReviewIssue` models are the boundary types
for the workflow's own input/output; they are not redefined here.
"""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field

from ..models import CodeReviewIssue, ReviewChunk


class ReviewPrepDTO(BaseModel):
    """Output of the prepare activity — everything the map phase needs.

    Invariants:
        - When ``no_code`` is True the review is already decided: ``chunks`` is
          empty and ``skipped_issues`` (info findings for empty files) is the
          whole result. The workflow returns an approved empty verdict without
          fanning out.
        - ``base_input`` carries the shared ``ChunkReviewInput`` fields (with
          ``profile`` normalized to its enum ``.value`` so the payload is
          JSON-native); ``context_fp`` fingerprints those shared fields plus the
          resolved model, and ``surface_by_path`` is the whole submission's
          cross-file symbol surface — both fed unchanged into every map call.
        - ``fanout_width`` is this review's own adaptive map-phase concurrency
          cap (``config.resolve_temporal_fanout_width(len(chunks))``): at most
          this many ``review_chunk_activity`` calls run concurrently for this
          review, regardless of ``len(chunks)``.
        - ``single_chunk`` is ``True`` iff ``len(chunks) == 1`` after prepare
          (a convenience flag for the workflow / map phase; it does not change
          the payload shape — ``chunks`` still holds that one entry).
    """

    no_code: bool = False
    skipped_issues: List[CodeReviewIssue] = Field(default_factory=list)
    chunks: List[ReviewChunk] = Field(default_factory=list)
    base_input: Dict[str, Any] = Field(default_factory=dict)
    context_fp: str = ""
    surface_by_path: Dict[str, List[str]] = Field(default_factory=dict)
    single_chunk: bool = False
    fanout_width: int = 1


class ChunkOutcomeDTO(BaseModel):
    """JSON-native mirror of ``mapping._ChunkOutcome`` for one map call.

    Invariants:
        - Field-for-field parallel to the private dataclass: ``approved_flags``
          holds one entry per successful sub-review, degraded "not reviewed"
          coverage findings live only in ``not_reviewed_issues``, and genuine
          reviewer findings live only in ``issues`` — the same separation the
          reduce phase relies on. ``degraded_recovery`` mirrors the dataclass
          flag (cached entries never set it; included for Temporal round-trips).
    """

    issues: List[CodeReviewIssue] = Field(default_factory=list)
    not_reviewed_issues: List[CodeReviewIssue] = Field(default_factory=list)
    summaries: List[str] = Field(default_factory=list)
    spec_notes: List[str] = Field(default_factory=list)
    approved_flags: List[bool] = Field(default_factory=list)
    degraded_recovery: bool = False

    @classmethod
    def from_outcome(cls, outcome: Any) -> "ChunkOutcomeDTO":
        """Build a DTO from a ``mapping._ChunkOutcome``.

        Preconditions:
            - ``outcome`` exposes the five ``_ChunkOutcome`` list fields (and
              optionally ``degraded_recovery``).

        Postconditions:
            - Returns a DTO whose lists equal the outcome's, with issues copied
              as fresh ``CodeReviewIssue`` models (no shared mutable state).
        """
        return cls(
            issues=[i.model_copy(deep=True) for i in outcome.issues],
            not_reviewed_issues=[i.model_copy(deep=True) for i in outcome.not_reviewed_issues],
            summaries=list(outcome.summaries),
            spec_notes=list(outcome.spec_notes),
            approved_flags=list(outcome.approved_flags),
            degraded_recovery=bool(getattr(outcome, "degraded_recovery", False)),
        )

    def to_outcome(self) -> Any:
        """Rebuild a ``mapping._ChunkOutcome`` from this DTO.

        Postconditions:
            - Returns a fresh ``_ChunkOutcome`` with deep-copied issues and no
              shared mutable state with ``self``. Lazy-imports the dataclass so
              this module stays importable from Temporal workers without a
              circular dependency on ``mapping``.
        """
        from ..mapping import _ChunkOutcome  # noqa: PLC0415

        return _ChunkOutcome(
            issues=[i.model_copy(deep=True) for i in self.issues],
            not_reviewed_issues=[i.model_copy(deep=True) for i in self.not_reviewed_issues],
            summaries=list(self.summaries),
            spec_notes=list(self.spec_notes),
            approved_flags=list(self.approved_flags),
            degraded_recovery=self.degraded_recovery,
        )
