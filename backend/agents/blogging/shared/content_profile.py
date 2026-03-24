"""
Content profiles for guideline-based blog length and scope.

Callers choose a semantic format (e.g. short listicle vs technical deep dive). The pipeline
resolves that to target/soft word bands, editor thresholds, and prompt text. Optional
``target_word_count`` overrides the numeric target only; profile still shapes guidance and
editor strictness.

Precedence:
1. If ``target_word_count`` is set, it becomes ``LengthPolicy.target_word_count`` (clamped
   100–10_000). Soft min/max are derived proportionally from that target.
2. If ``target_word_count`` is omitted, targets come from the selected ``ContentProfile``.
3. If ``content_profile`` is omitted, ``standard_article`` is used (legacy default ~1000 words).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, NamedTuple, Optional

from pydantic import BaseModel, Field


class ContentProfile(str, Enum):
    """High-level writing format; drives length guidance and editor behavior."""

    short_listicle = "short_listicle"
    standard_article = "standard_article"
    technical_deep_dive = "technical_deep_dive"
    series_instalment = "series_instalment"


class SeriesContext(BaseModel):
    """When the post is one part of a series — scopes outline and draft to this instalment."""

    series_title: str = Field("", description="Overall series name or theme.")
    part_number: int = Field(1, ge=1, description="This instalment number (1-based).")
    planned_parts: Optional[int] = Field(
        None,
        ge=2,
        description="Total planned posts in the series, if known.",
    )
    instalment_scope: Optional[str] = Field(
        None,
        description="What this post covers; later posts should defer other topics.",
    )


class _Preset(NamedTuple):
    target: int
    soft_min: int
    soft_max: int
    must_fix_over_ratio: float
    should_fix_over_ratio: float


_PRESETS: dict[ContentProfile, _Preset] = {
    ContentProfile.short_listicle: _Preset(750, 500, 1100, 1.45, 1.22),
    ContentProfile.standard_article: _Preset(1000, 750, 1300, 1.30, 1.10),
    ContentProfile.technical_deep_dive: _Preset(2200, 1500, 3200, 1.25, 1.08),
    ContentProfile.series_instalment: _Preset(1400, 950, 2000, 1.30, 1.10),
}

_GUIDANCE: dict[ContentProfile, str] = {
    ContentProfile.short_listicle: (
        "Format: **Short, scannable listicle / high-level explainer**.\n"
        "- Prioritize skim-friendly structure: clear headings, short sections, bullets where they help.\n"
        "- Stay concise: one key idea per section; avoid exhaustive tutorials or edge-case deep dives.\n"
        "- Aim for practical takeaways readers can absorb in one sitting."
    ),
    ContentProfile.standard_article: (
        "Format: **Standard long-form article**.\n"
        "- Balance narrative flow with clear section structure.\n"
        "- Enough depth to be useful, without turning into a full manual.\n"
        "- One coherent arc: hook, development, actionable conclusion."
    ),
    ContentProfile.technical_deep_dive: (
        "Format: **Technical deep dive**.\n"
        "- Substantive detail: precise explanations, trade-offs, and (where relevant) steps or examples.\n"
        "- Longer sections are acceptable when they add real technical value — avoid padding.\n"
        "- Assume a reader who wants to understand *how* and *why*, not just headlines."
    ),
    ContentProfile.series_instalment: (
        "Format: **Series instalment** (this post is one part of a multi-part arc).\n"
        "- Cover only what belongs in *this* instalment; explicitly defer other topics to future posts.\n"
        "- Open with brief context for readers who may start here; end with a light bridge to the next part.\n"
        "- Do not try to deliver the entire series in one article."
    ),
}


class LengthPolicy(BaseModel):
    """Resolved length and editor policy for one pipeline run."""

    content_profile: ContentProfile
    target_word_count: int = Field(..., ge=100, le=10000)
    soft_min_words: int = Field(..., ge=100, le=10000)
    soft_max_words: int = Field(..., ge=100, le=10000)
    length_guidance: str = Field(
        ...,
        description="Paragraph(s) for draft, review, and copy-editor prompts.",
    )
    editor_must_fix_over_ratio: float = Field(1.30, ge=1.0, le=3.0)
    editor_should_fix_over_ratio: float = Field(1.10, ge=1.0, le=2.0)


def _series_block(ctx: SeriesContext) -> str:
    parts = [
        "\n\n**Series context (apply to outline and draft):**",
        f"- Series: {ctx.series_title or '(untitled series)'}",
        f"- This instalment: Part {ctx.part_number}",
    ]
    if ctx.planned_parts is not None:
        parts.append(f"- Planned series length: {ctx.planned_parts} posts (approximate).")
    if ctx.instalment_scope and ctx.instalment_scope.strip():
        parts.append(f"- Scope of *this* post only: {ctx.instalment_scope.strip()}")
    parts.append(
        "- The outline must label deferred topics for later instalments; do not exhaust the whole series here."
    )
    return "\n".join(parts)


def resolve_length_policy(
    *,
    content_profile: Optional[ContentProfile] = None,
    explicit_target_word_count: Optional[int] = None,
    length_notes: Optional[str] = None,
    series_context: Optional[SeriesContext] = None,
) -> LengthPolicy:
    """
    Build a LengthPolicy from optional profile, explicit word target, and notes.

    See module docstring for precedence rules.
    """
    effective_profile = content_profile or ContentProfile.standard_article
    preset = _PRESETS[effective_profile]

    if explicit_target_word_count is not None:
        target = max(100, min(10_000, int(explicit_target_word_count)))
        soft_min = max(100, int(target * 0.75))
        soft_max = min(10_000, int(target * 1.35))
        must_r = preset.must_fix_over_ratio
        should_r = preset.should_fix_over_ratio
    else:
        target = preset.target
        soft_min = preset.soft_min
        soft_max = preset.soft_max
        must_r = preset.must_fix_over_ratio
        should_r = preset.should_fix_over_ratio

    if soft_max < target:
        soft_max = target
    if soft_min > target:
        soft_min = max(100, target - 200)

    guidance = _GUIDANCE[effective_profile]
    if series_context is not None:
        guidance = guidance + _series_block(series_context)
    if length_notes and length_notes.strip():
        guidance += "\n\n**Author notes (length / scope):**\n" + length_notes.strip()

    return LengthPolicy(
        content_profile=effective_profile,
        target_word_count=target,
        soft_min_words=soft_min,
        soft_max_words=soft_max,
        length_guidance=guidance,
        editor_must_fix_over_ratio=must_r,
        editor_should_fix_over_ratio=should_r,
    )


def build_draft_length_instruction(policy: LengthPolicy) -> str:
    """Single block for draft/revise prompts: qualitative guidance + numeric band."""
    return (
        f"CONTENT PROFILE: {policy.content_profile.value.replace('_', ' ')}\n\n"
        f"{policy.length_guidance}\n\n"
        f"TARGET LENGTH: Aim for roughly **{policy.target_word_count}** words "
        f"(acceptable band about **{policy.soft_min_words}–{policy.soft_max_words}** words). "
        "Hit the intent of the profile first — do not pad to reach the number, and do not cut "
        "necessary substance. Prefer landing near the target when it matches the profile."
    )


def series_context_block(ctx: Optional[SeriesContext]) -> Optional[str]:
    """Return series instalment block for planning/draft prompts, or None."""
    if ctx is None:
        return None
    return _series_block(ctx)


def build_planning_length_context(policy: LengthPolicy) -> str:
    """Context for planning prompts: profile, word band, and qualitative guidance."""
    return (
        f"CONTENT PROFILE: {policy.content_profile.value.replace('_', ' ')}\n"
        f"Approximate word target for the finished post: {policy.target_word_count} "
        f"(soft range {policy.soft_min_words}–{policy.soft_max_words}). "
        "Shape the plan so a draft at this depth fits the profile — not a treatise if the profile "
        "is short, and not a thin plan if the profile is a deep dive.\n\n"
        f"{policy.length_guidance}"
    )


def build_review_length_context(policy: LengthPolicy) -> str:
    """Alias for :func:`build_planning_length_context` (legacy name)."""
    return build_planning_length_context(policy)


def resolve_length_policy_from_request_dict(request_dict: Dict[str, Any]) -> LengthPolicy:
    """Parse Temporal/API JSON dict into a LengthPolicy (defaults match legacy 1000-word standard)."""
    profile: Optional[ContentProfile] = None
    raw_profile = request_dict.get("content_profile")
    if raw_profile is not None and raw_profile != "":
        profile = (
            raw_profile
            if isinstance(raw_profile, ContentProfile)
            else ContentProfile(str(raw_profile))
        )

    series: Optional[SeriesContext] = None
    raw_series = request_dict.get("series_context")
    if raw_series:
        series = (
            raw_series
            if isinstance(raw_series, SeriesContext)
            else SeriesContext.model_validate(raw_series)
        )

    notes_raw = request_dict.get("length_notes")
    notes: Optional[str] = None
    if isinstance(notes_raw, str) and notes_raw.strip():
        notes = notes_raw.strip()

    twc_raw = request_dict.get("target_word_count")
    twc: Optional[int] = None
    if twc_raw is not None and twc_raw != "":
        twc = int(twc_raw)

    return resolve_length_policy(
        content_profile=profile,
        explicit_target_word_count=twc,
        length_notes=notes,
        series_context=series,
    )
