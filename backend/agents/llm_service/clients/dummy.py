"""
Dummy LLM client for tests and environments without an LLM.

Returns heuristic stub responses matching SE team prompts so existing tests keep passing.

Strands ``Model`` compatibility is opt-in and lazy: importing / using this module as a
plain :class:`~llm_service.interface.LLMClient` does not import ``strands``. Concrete
``Model`` members that ``strands.Agent`` reads at construction time (``stateful``,
``context_window_limit``, ``count_tokens``) are provided here without loading Strands.
Call :func:`ensure_strands_model_registration` (or any Strands Model ABC method, or
construct ``DummyLLMClient`` after ``strands`` is already imported) when callers need
real ``isinstance(..., Model)`` / MRO inheritance.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import AsyncGenerator, AsyncIterable
from typing import TYPE_CHECKING, Any, Dict, Optional

from ..interface import LLMClient

if TYPE_CHECKING:  # pragma: no cover - typing only; runtime uses lazy strands imports
    from strands.types.content import Message as StrandsMessage
    from strands.types.content import SystemContentBlock
    from strands.types.streaming import StreamEvent
    from strands.types.tools import ToolChoice, ToolSpec

_STRANDS_MODEL_REGISTERED = False


def _strands_already_imported() -> bool:
    """Return True when any ``strands`` package module is already in ``sys.modules``.

    Preconditions: none.
    Postconditions: returns True iff ``strands`` or a ``strands.*`` module is loaded.
    """
    # Snapshot keys: concurrent imports can mutate ``sys.modules`` mid-iteration.
    return "strands" in sys.modules or any(
        name.startswith("strands.") for name in tuple(sys.modules)
    )


def ensure_strands_model_registration() -> None:
    """Attach Strands ``Model`` as a real base of :class:`DummyLLMClient`.

    Virtual ``Model.register()`` is intentionally *not* used: ``Agent(model=...)``
    reads concrete ``Model`` members (``stateful``, ``count_tokens``, …) before any
    model method runs, and virtual subclasses do not inherit those members.

    Preconditions:
        The ``strands`` package is installed and importable.
    Postconditions:
        ``isinstance(DummyLLMClient(), strands.models.model.Model)`` is True and
        ``Model`` appears in ``DummyLLMClient.__mro__``, including for instances
        constructed before Strands was imported (ABC negative caches are cleared).
        Idempotent: subsequent calls are no-ops once inheritance is attached.
    """
    global _STRANDS_MODEL_REGISTERED
    if _STRANDS_MODEL_REGISTERED:
        return
    from strands.models.model import Model  # noqa: PLC0415 - intentional lazy import

    # Real inheritance (not ABC.register) so instances expose Model's concrete
    # properties/methods, matching the pre-lazy ``(LLMClient, Model)`` bases.
    if Model not in DummyLLMClient.__mro__:
        DummyLLMClient.__bases__ = (*DummyLLMClient.__bases__, Model)
        # Mutating ``__bases__`` does not bump ABCMeta's invalidation counter, so a
        # prior negative ``isinstance(client, Model)`` / ``issubclass(...)`` result
        # stays cached forever. Clear Model's ABC caches so the postcondition holds
        # for instances constructed before Strands was imported.
        Model._abc_caches_clear()
    _STRANDS_MODEL_REGISTERED = True


# Prompts this long that still mention "approved" are treated as code-review
# chunks (full review context) rather than short approval-only stubs.
CODE_REVIEW_MIN_PROMPT_LENGTH = 200

_STRIP_VERBS: frozenset[str] = frozenset(
    {
        "implement",
        "create",
        "build",
        "add",
        "setup",
        "set",
        "up",
        "configure",
        "make",
        "define",
        "develop",
        "write",
        "design",
        "establish",
        "generate",
        "fetches",
        "displays",
        "handles",
        "manages",
        "processes",
        "returns",
        "provides",
        "supports",
        "includes",
        "enables",
        "renders",
    }
)
_STRIP_FILLERS: frozenset[str] = frozenset(
    {
        "the",
        "that",
        "with",
        "using",
        "which",
        "for",
        "and",
        "a",
        "an",
        "to",
        "of",
        "in",
        "on",
        "by",
        "from",
        "into",
        "as",
        "via",
        "its",
        "all",
        "application",
        "system",
        "project",
        "based",
        "proper",
        "production",
        "quality",
        "complete",
        "full",
        "new",
        "existing",
        "angular",
        "react",
        "vue",
        "spring",
        "fastapi",
        "flask",
        "django",
    }
)
_STRIP_SUFFIXES: frozenset[str] = frozenset(
    {
        "component",
        "service",
        "module",
        "endpoint",
        "endpoints",
        "middleware",
        "guard",
        "pipe",
        "directive",
        "interceptor",
        "controller",
        "repository",
    }
)


def _placeholder_slug(hint: str, separator: str, max_length: int) -> str:
    """Build a unique fallback slug when filtering leaves no usable words.

    Preconditions:
        - ``hint`` / ``separator`` are strings; ``separator`` is non-empty.
        - ``max_length`` is a positive integer.

    Postconditions:
        - Returns a non-empty identifier of at most ``max_length`` characters
          whose digest portion is derived from ``hint``, so distinct hints
          produce distinct placeholders.
    """
    digest = hashlib.md5(hint.encode(), usedforsecurity=False).hexdigest()[:8]
    result = f"item{separator}{digest}"
    if len(result) > max_length:
        result = result[:max_length].rstrip(separator)
    # Fallback must also respect max_length: if separator shares a character
    # with the literal "item" prefix, truncation above can strip result to "".
    return result or f"item{separator}0"[:max_length]


def _extract_name_from_hint(hint: str, separator: str = "-", max_length: int = 25) -> str:
    """Derive a short, identifier-friendly name from a free-form hint.

    Strips common verbs, filler words, and type suffixes, keeps up to the
    first three remaining words, joins them with ``separator``, and
    truncates to ``max_length``.

    Preconditions:
        - ``hint`` is a string (may be empty; empty / all-stripped yields a
          hint-derived placeholder).
        - ``separator`` is a non-empty string safe for identifiers.
        - ``max_length`` is a positive integer.

    Postconditions:
        - Returns a non-empty string of at most ``max_length`` characters.
        - The result contains only lowercase alphanumerics and ``separator``.
        - When filtering leaves no usable words, the placeholder is derived
          from a digest of ``hint`` so distinct all-stripped hints do not
          collapse onto one path.
    """
    assert isinstance(hint, str)
    assert isinstance(separator, str) and separator
    assert isinstance(max_length, int) and max_length > 0

    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", hint)
    words = re.sub(r"[^a-z0-9\s]+", " ", expanded.lower()).split()
    filtered = [
        w
        for w in words
        if w not in _STRIP_VERBS and w not in _STRIP_FILLERS and w not in _STRIP_SUFFIXES
    ]
    if not filtered:
        # Do not reintroduce verbs/fillers/suffixes; stay unique per hint.
        return _placeholder_slug(hint, separator, max_length)
    result = separator.join(filtered[:3])
    if len(result) > max_length:
        result = result[:max_length].rstrip(separator)
    return result or _placeholder_slug(hint, separator, max_length)


def _content_to_text(content: Any) -> str:
    """Flatten a message ``content`` field into newline-joined text.

    Recognizes ``text``, bare strings, nested ``toolResult``, and ``json``
    blocks (serialized like ``_tool_result_content_to_text`` in the adapter).

    Preconditions:
        - ``content`` is a string, a list of Strands content blocks, or other.

    Postconditions:
        - Returns a string (empty when no text can be extracted).
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and "text" in block:
            parts.append(str(block["text"]))
        elif isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and "json" in block:
            parts.append(json.dumps(block["json"]))
        elif isinstance(block, dict) and "toolResult" in block:
            tr = block.get("toolResult") or {}
            nested = _content_to_text(tr.get("content", []))
            if nested:
                parts.append(nested)
    return "\n".join(parts)


def _last_user_text(messages: list) -> str:
    """Return concatenated text from the most recent user message.

    Used by ``stream()``. Multi-block text within that single turn is
    newline-joined so routing anchors in later blocks are not dropped.

    Preconditions:
        - ``messages`` is a list of Strands-style message dicts (role/content).

    Postconditions:
        - Returns a string (empty when no user text is present).
        - When the latest user message has multiple text blocks, all of them
          appear in the returned string (newline-joined).
    """
    assert isinstance(messages, list)
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        return _content_to_text(msg.get("content", []))
    return ""


def _aggregated_user_tool_text(messages: list) -> str:
    """Join all user/tool turn texts for structured-output routing.

    Mirrors ``LLMClientModel.structured_output``, which aggregates converted
    user and tool message contents so follow-up turns like "return that as
    structured output" still see routing anchors from earlier requests.
    ``stream()`` continues to use ``_last_user_text`` (latest turn only).

    Preconditions:
        - ``messages`` is a list of Strands-style message dicts (role/content).

    Postconditions:
        - Returns a string (empty when no user/tool text is present).
        - Non-empty user/tool turns appear in order, separated by blank lines.
    """
    assert isinstance(messages, list)
    parts: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") not in ("user", "tool"):
            continue
        text = _content_to_text(msg.get("content", []))
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _flatten_system_prompt_content(
    system_prompt_content: list[SystemContentBlock] | None,
) -> str:
    """Flatten Strands system content blocks into a single prompt string.

    Preconditions:
        - ``system_prompt_content`` is ``None`` or a list of content blocks.

    Postconditions:
        - Returns concatenated text from blocks (empty when absent).
    """
    if not system_prompt_content:
        return ""
    parts: list[str] = []
    for block in system_prompt_content:
        if isinstance(block, dict):
            parts.append(str(block.get("text", "") or ""))
        else:
            parts.append(str(block))
    return "".join(parts)


def _branding_phase3_structured_stub(system_lowered: str) -> Optional[Dict[str, Any]]:
    """Return a Phase 3 agent structured-output stub, or ``None`` if unmatched.

    Preconditions:
        ``system_lowered`` is the agent system prompt already lowercased (may be empty).
    Postconditions:
        Returns a dict that validates against the matching Phase 3 agent
        ``structured_output`` schema, or ``None`` when no Phase 3 agent matches.
        Covers all ten Phase 3 factories: CreativeDirector, MoodBoardConceptualist,
        ConvergeDecider, and the seven post-converge specialists
        (logo_specifier, color_system_builder, typography_builder,
        iconography_director, photography_video_director, voice_tone_builder,
        design_system_codifier). Specialist branches match on lowercased prompt
        *substrings*, not factory names: logo_specifier matches "logo specifier"
        (space), color_system_builder matches "color system builder", and
        typography_builder matches "typography builder". The remaining four
        specialists (iconography_director, photography_video_director,
        voice_tone_builder, design_system_codifier) have no name-substring
        anchor at all — they match on output-field names only.

    Ordering constraints (first three only — do not conflate them):
        1. CreativeDirector — ``mood_board_candidates`` + ``converge_decider``
           → ``MoodBoardCandidatesOutput`` (must precede MoodBoardConceptualist
           because its prompt also names the moodboard field list).
        2. MoodBoardConceptualist — ``moodboard conceptualist`` + ``visual_direction``
           → ``MoodBoardConceptOutput``.
        3. ConvergeDecider — ``winning_candidate_title`` + ``scores_by_candidate``
           → ``CreativeRefinementDecisionOutput`` (separate from CreativeDirector).
        Specialists are matched afterward by agent-specific prompt anchors.
    """
    if "mood_board_candidates" in system_lowered and "converge_decider" in system_lowered:
        return {
            "mood_board_candidates": [
                {
                    "title": "Editorial Clarity",
                    "visual_direction": "Quiet editorial layouts with generous whitespace (dummy).",
                    "color_story": ["Ink black", "Warm ivory", "Accent rust"],
                    "typography_direction": "Serif display with clean sans body (dummy).",
                    "image_style": ["Documentary stills", "Soft natural light"],
                },
                {
                    "title": "Minimal Signal",
                    "visual_direction": "Sparse geometry and high-contrast marks (dummy).",
                    "color_story": ["Charcoal", "Paper white", "Signal blue"],
                    "typography_direction": "Single sans family with tight tracking (dummy).",
                    "image_style": ["Product-on-void", "Hard shadows"],
                },
                {
                    "title": "Bold Momentum",
                    "visual_direction": "Large type and energetic color blocks (dummy).",
                    "color_story": ["Electric coral", "Deep navy", "Near-black"],
                    "typography_direction": "Heavy display sans with mono captions (dummy).",
                    "image_style": ["Motion blur", "Saturated lifestyle"],
                },
            ]
        }
    if "moodboard conceptualist" in system_lowered and "visual_direction" in system_lowered:
        return {
            "title": "Dummy Moodboard Direction",
            "visual_direction": "Cohesive visual system for Dummy Co. (dummy).",
            "color_story": ["Primary ink", "Support gray", "Accent teal"],
            "typography_direction": "Modern sans with restrained serif accents (dummy).",
            "image_style": ["Clean product photography", "Soft gradients", "Human scale"],
        }
    if "winning_candidate_title" in system_lowered and "scores_by_candidate" in system_lowered:
        return {
            "winning_candidate_title": "Editorial Clarity",
            "scoring_criteria": [
                "Audience resonance",
                "Distinctiveness",
                "Cross-channel consistency",
                "Execution feasibility",
            ],
            "scores_by_candidate": {
                "Editorial Clarity": 0.91,
                "Minimal Signal": 0.78,
                "Bold Momentum": 0.74,
            },
            "rationale": "Editorial Clarity best matches Dummy Co.'s clarity promise (dummy).",
            "workshop_prompts": [
                "Which candidate feels most like us?",
                "Where would this break in product UI?",
                "What must stay constant across channels?",
            ],
            "decision_criteria": [
                "Matches positioning",
                "Feasible in 90 days",
                "Works in dark and light UI",
            ],
        }
    if "logo specifier" in system_lowered and "clear_space" in system_lowered:
        return {
            "logo_suite": [
                {
                    "variant": "primary",
                    "usage_context": "Default lockup on light backgrounds (dummy).",
                    "minimum_size": "24px height",
                    "clear_space": "0.5x logo height",
                },
                {
                    "variant": "monochrome",
                    "usage_context": "Single-color print and embroidery (dummy).",
                    "minimum_size": "24px height",
                    "clear_space": "0.5x logo height",
                },
                {
                    "variant": "icon-only",
                    "usage_context": "App icons and favicons (dummy).",
                    "minimum_size": "16px",
                    "clear_space": "0.25x icon width",
                },
                {
                    "variant": "reversed",
                    "usage_context": "Dark backgrounds and photography overlays (dummy).",
                    "minimum_size": "24px height",
                    "clear_space": "0.5x logo height",
                },
            ]
        }
    if "psychological_rationale" in system_lowered and "color system builder" in system_lowered:
        return {
            "color_palette": [
                {
                    "name": "Ink",
                    "hex_value": "#111827",
                    "usage": "Primary text and logos",
                    "psychological_rationale": "Signals clarity and confidence (dummy).",
                },
                {
                    "name": "Paper",
                    "hex_value": "#F8FAFC",
                    "usage": "Surfaces and backgrounds",
                    "psychological_rationale": "Keeps interfaces calm (dummy).",
                },
                {
                    "name": "Signal",
                    "hex_value": "#0EA5E9",
                    "usage": "Accent CTAs",
                    "psychological_rationale": "Draws attention without alarm (dummy).",
                },
                {
                    "name": "Support",
                    "hex_value": "#64748B",
                    "usage": "Secondary text",
                    "psychological_rationale": "Hierarchy without noise (dummy).",
                },
                {
                    "name": "Critical",
                    "hex_value": "#DC2626",
                    "usage": "Errors and destructive actions",
                    "psychological_rationale": "Clear urgency cue (dummy).",
                },
            ]
        }
    if "typography builder" in system_lowered and "weight_range" in system_lowered:
        return {
            "typography_system": [
                {
                    "role": "display",
                    "font_family": "Inter Display",
                    "weight_range": "600-700",
                    "usage_notes": "Hero headlines only (dummy).",
                },
                {
                    "role": "body",
                    "font_family": "Inter",
                    "weight_range": "400-500",
                    "usage_notes": "Long-form and UI copy (dummy).",
                },
                {
                    "role": "caption",
                    "font_family": "Inter",
                    "weight_range": "400-500",
                    "usage_notes": "Meta labels and footnotes (dummy).",
                },
            ]
        }
    if "iconography_style" in system_lowered and "illustration_style" in system_lowered:
        return {
            "iconography_style": (
                "2px stroke, 2px corner radius, limited fill — geometric and calm (dummy)."
            ),
            "illustration_style": (
                "Flat editorial scenes with restrained gradients and human scale (dummy)."
            ),
        }
    if "photography_direction" in system_lowered and "motion_principles" in system_lowered:
        return {
            "photography_direction": (
                "Natural light, documentary framing, real product in use (dummy)."
            ),
            "video_direction": "Steady pacing, soft cuts, voice-forward demos (dummy).",
            "motion_principles": [
                "Ease-out entrances",
                "Prefer opacity over bounce",
                "Keep durations under 240ms for UI",
            ],
        }
    if "voice_tone_spectrum" in system_lowered and "language_donts" in system_lowered:
        return {
            "voice_tone_spectrum": [
                {
                    "context": "marketing",
                    "tone": "Confident and concrete",
                    "examples": ["Ship brand with the product", "Clarity over slogans"],
                },
                {
                    "context": "support",
                    "tone": "Calm and helpful",
                    "examples": ["Here is the next step", "We can fix that together"],
                },
                {
                    "context": "legal",
                    "tone": "Precise and plain",
                    "examples": ["This agreement covers", "You may opt out"],
                },
                {
                    "context": "social",
                    "tone": "Human and brief",
                    "examples": ["Shipped this week", "Ask us anything"],
                },
                {
                    "context": "internal",
                    "tone": "Direct and collaborative",
                    "examples": ["Decision needed by Friday", "Proposal attached"],
                },
            ],
            "language_dos": [
                "Lead with the customer outcome (dummy).",
                "Use active voice (dummy).",
                "Name the proof point (dummy).",
                "Keep sentences scannable (dummy).",
            ],
            "language_donts": [
                "Avoid empty superlatives (dummy).",
                "Don't bury the offer (dummy).",
                "Don't invent category jargon (dummy).",
                "Don't mix slang with legal claims (dummy).",
            ],
        }
    if "foundation_tokens" in system_lowered and "component_standards" in system_lowered:
        return {
            "design_principles": [
                "Clarity over decoration (dummy).",
                "Consistency enables speed (dummy).",
                "Every state must be intentional (dummy).",
            ],
            "foundation_tokens": [
                "color",
                "type",
                "spacing",
                "motion",
                "elevation",
            ],
            "component_standards": [
                "Buttons: one primary action per view (dummy).",
                "Cards: 16px padding, single accent (dummy).",
                "Navigation: persistent labels, no icon-only primary nav (dummy).",
            ],
        }
    return None


def _branding_phase4_structured_stub(system_lowered: str) -> Optional[Dict[str, Any]]:
    """Return a Phase 4 agent structured-output stub, or ``None`` if unmatched.

    Preconditions:
        ``system_lowered`` is the agent system prompt already lowercased (may be empty).
    Postconditions:
        Returns a dict that validates against the matching Phase 4 agent
        ``structured_output`` schema, or ``None`` when no Phase 4 agent matches.
        Covers all four distinct Phase 4 schemas: ``brand_experience_principler``,
        the six ``_make_channel_guide`` agents (all share ``ChannelGuidelineOutput``,
        so one stub branch covers all six), ``brand_architecture_builder``, and
        ``brand_in_action_illustrator``.
    """
    if "content_types" in system_lowered and "frequency_guidance" in system_lowered:
        channel_match = re.search(r"channel:\s*'([a-z_]+)'", system_lowered)
        channel_value = channel_match.group(1) if channel_match else "channel"
        return {
            "channel": channel_value,
            "strategy": f"Lead with proof points tailored to the {channel_value} audience (dummy).",
            "dos": [
                "Match the channel's native format (dummy).",
                "Lead with the strongest proof point (dummy).",
                "Keep a consistent voice across posts (dummy).",
            ],
            "donts": [
                "Don't repurpose copy verbatim from other channels (dummy).",
                "Don't bury the call to action (dummy).",
                "Don't ignore channel-specific limits (dummy).",
            ],
            "content_types": [
                "Short-form updates (dummy).",
                "Case study highlights (dummy).",
                "Behind-the-scenes moments (dummy).",
            ],
            "frequency_guidance": "Publish on a predictable weekly cadence (dummy).",
        }
    if "brand_experience_principles" in system_lowered and "sensory_elements" in system_lowered:
        return {
            "brand_experience_principles": [
                "Every touchpoint should feel intentional (dummy).",
                "Consistency builds trust over time (dummy).",
                "Speed should never break polish (dummy).",
            ],
            "signature_moments": [
                "First login walkthrough (dummy).",
                "Onboarding welcome email (dummy).",
                "Renewal confirmation moment (dummy).",
            ],
            "sensory_elements": [
                "Confident, low-pitched notification chime (dummy).",
                "Matte, tactile packaging texture (dummy).",
            ],
        }
    if "brand_architecture" in system_lowered and "terminology_glossary" in system_lowered:
        return {
            "brand_architecture": [
                {
                    "entity": "parent brand",
                    "relationship": "Umbrella over all products (dummy).",
                    "naming_convention": "Dummy Co. + [Product] (dummy).",
                    "visual_treatment": "Shared wordmark, distinct accent color (dummy).",
                }
            ],
            "naming_conventions": [
                "Product names are one word (dummy).",
                "Avoid internal codenames externally (dummy).",
                "Always pair sub-brand with parent brand on first mention (dummy).",
            ],
            "terminology_glossary": {
                "brand architecture": "How parent and sub-brands relate (dummy).",
                "sub-brand": "A named offering under the parent brand (dummy).",
                "wordmark": "The brand's logotype (dummy).",
                "boilerplate": "Standard company description (dummy).",
                "voice": "How the brand sounds in writing (dummy).",
            },
        }
    if "correct_example" in system_lowered and "incorrect_example" in system_lowered:
        return {
            "brand_in_action": [
                {
                    "context": "Sales deck header (dummy).",
                    "correct_example": "Uses the approved wordmark and tagline (dummy).",
                    "incorrect_example": "Stretches the logo and adds a drop shadow (dummy).",
                    "rationale": "Keeps the mark legible and on-brand (dummy).",
                },
                {
                    "context": "Support email signature (dummy).",
                    "correct_example": "Plain-text signature with the approved title (dummy).",
                    "incorrect_example": "Adds an unapproved emoji and banner image (dummy).",
                    "rationale": "Matches the calm, helpful support tone (dummy).",
                },
                {
                    "context": "Social post header (dummy).",
                    "correct_example": "Uses the brand accent color and approved crop (dummy).",
                    "incorrect_example": "Uses an off-palette gradient background (dummy).",
                    "rationale": "Preserves visual consistency across channels (dummy).",
                },
            ]
        }
    return None


def _branding_phase5_structured_stub(system_lowered: str) -> Optional[Dict[str, Any]]:
    """Return a Phase 5 agent structured-output stub, or ``None`` if unmatched.

    Preconditions:
        ``system_lowered`` is the agent system prompt already lowercased (may be empty).
    Postconditions:
        Returns a dict that validates against the matching Phase 5 agent
        ``structured_output`` schema, or ``None`` when no Phase 5 agent matches.
        Covers all 7 Phase 5 factories: ``ownership_definer``,
        ``approval_workflow_designer``, ``asset_wiki_planner``,
        ``training_planner``, ``kpi_designer``, ``evolution_framer``, and
        ``brand_rules_codifier``.
    """
    if "ownership_model" in system_lowered and "decision_authority" in system_lowered:
        return {
            "ownership_model": (
                "The Brand Director owns final say on all brand decisions, with input from "
                "Marketing and Product leads (dummy)."
            ),
            "decision_authority": {
                "logo_changes": "Brand Director",
                "campaign_messaging": "Marketing Lead",
                "product_naming": "Product Lead",
            },
        }
    if "approval_workflows" in system_lowered and "agency_briefing_protocols" in system_lowered:
        return {
            "approval_workflows": [
                {
                    "asset_type": "Logo usage",
                    "approvers": ["Brand Director"],
                    "sla": "2 business days",
                    "escalation_path": "Escalate to CMO after 3 days (dummy).",
                },
                {
                    "asset_type": "Campaign messaging",
                    "approvers": ["Marketing Lead", "Brand Director"],
                    "sla": "3 business days",
                    "escalation_path": "Escalate to CMO after 5 days (dummy).",
                },
                {
                    "asset_type": "Product naming",
                    "approvers": ["Product Lead", "Brand Director"],
                    "sla": "5 business days",
                    "escalation_path": "Escalate to VP Product after 7 days (dummy).",
                },
            ],
            "agency_briefing_protocols": [
                "Share the brand guidelines doc before kickoff (dummy).",
                "Require a written creative brief signed off by the Brand Director (dummy).",
                "Hold a kickoff call covering voice, tone, and visual do's/don'ts (dummy).",
            ],
        }
    if "asset_management_guidance" in system_lowered and "wiki_backlog" in system_lowered:
        return {
            "asset_management_guidance": [
                "Store all approved assets in the central DAM (dummy).",
                "Archive deprecated assets instead of deleting them (dummy).",
                "Tag every asset with its approval date and owner (dummy).",
            ],
            "wiki_backlog": [
                {
                    "title": "Brand North Star",
                    "summary": "One-page summary of purpose, vision, and positioning (dummy).",
                    "owners": ["Brand Director"],
                    "update_cadence": "quarterly",
                },
                {
                    "title": "Voice Playbook",
                    "summary": "Tone spectrum and language dos/don'ts (dummy).",
                    "owners": ["Brand Lead"],
                    "update_cadence": "quarterly",
                },
                {
                    "title": "Design System",
                    "summary": "Logo, color, typography, and component specs (dummy).",
                    "owners": ["Design Lead"],
                    "update_cadence": "monthly",
                },
                {
                    "title": "Brand Review Intake",
                    "summary": "How to submit assets for brand review (dummy).",
                    "owners": ["Brand Director"],
                    "update_cadence": "monthly",
                },
            ],
        }
    if "training_onboarding_plan" in system_lowered and "brand literacy" in system_lowered:
        return {
            "training_onboarding_plan": [
                "New-hire brand orientation session in week one (dummy).",
                "Quarterly brand refresher workshop (dummy).",
                "Self-serve brand guideline course in the LMS (dummy).",
                "Office-hours with the Brand team for open questions (dummy).",
            ],
        }
    if "brand_health_kpis" in system_lowered and "tracking_methodology" in system_lowered:
        return {
            "brand_health_kpis": [
                {
                    "metric": "Brand awareness",
                    "measurement_method": "Quarterly survey (dummy).",
                    "target": "60% aided awareness",
                    "review_frequency": "quarterly",
                },
                {
                    "metric": "Message consistency score",
                    "measurement_method": "Content audit against guidelines (dummy).",
                    "target": "90% compliant",
                    "review_frequency": "monthly",
                },
                {
                    "metric": "NPS",
                    "measurement_method": "Post-purchase survey (dummy).",
                    "target": "+40",
                    "review_frequency": "quarterly",
                },
                {
                    "metric": "Guideline adoption rate",
                    "measurement_method": "Percent of assets passing first-pass review (dummy).",
                    "target": "85%",
                    "review_frequency": "monthly",
                },
            ],
            "tracking_methodology": (
                "Combine quarterly surveys with ongoing content audits, reviewed in a monthly "
                "brand health dashboard (dummy)."
            ),
            "review_trigger_points": [
                "NPS drops more than 10 points quarter-over-quarter (dummy).",
                "A rebrand or major product launch is planned (dummy).",
                "Guideline adoption falls below 70% (dummy).",
            ],
        }
    if "evolution_framework" in system_lowered and "version_control_cadence" in system_lowered:
        return {
            "evolution_framework": (
                "The brand evolves incrementally through versioned updates, with major shifts "
                "reserved for strategic inflection points (dummy)."
            ),
            "version_control_cadence": (
                "Formal review every two quarters, with minor patches as needed (dummy)."
            ),
        }
    if "brand_guidelines" in system_lowered and "governance rules" in system_lowered:
        return {
            "brand_guidelines": [
                "Always use the approved wordmark; never recreate it (dummy).",
                "Lead every message with the customer outcome, not the feature (dummy).",
                "All external assets require Brand Director sign-off before release (dummy).",
                "Store approved assets only in the central DAM (dummy).",
                "Review the brand system every two quarters (dummy).",
            ],
        }
    return None


def _branding_phase2_narrative_base() -> Dict[str, Any]:
    """Return the base brand-narrative fields shared by all Phase 2 stubs.

    Preconditions:
        None.
    Postconditions:
        Returns a fresh dict with ``brand_story``, ``hero_narrative``, and
        ``boilerplate_variants`` — the fields every Phase 2 branding stub
        carries forward, regardless of how much further downstream fields
        each specific agent also requires.
    """
    return {
        "brand_story": (
            "Dummy Co. began when product teams kept shipping off-brand experiences. "
            "We built a system that keeps every touchpoint intentional (dummy)."
        ),
        "hero_narrative": "Brand experiences that ship with the product (dummy).",
        "boilerplate_variants": [
            "Dummy Co. helps teams ship on-brand (short).",
            "Dummy Co. turns brand strategy into consistent day-to-day execution (medium).",
            (
                "Dummy Co. partners with product organizations to make every customer "
                "touchpoint feel cohesive and intentional (long)."
            ),
        ],
    }


def _branding_phase2_narrative_with_archetype() -> Dict[str, Any]:
    """Return the Phase 2 base narrative plus ``brand_archetypes``.

    Preconditions:
        None.
    Postconditions:
        Returns a fresh dict extending ``_branding_phase2_narrative_base()``
        with a single-entry ``brand_archetypes`` list.
    """
    return {
        **_branding_phase2_narrative_base(),
        "brand_archetypes": [
            {
                "archetype": "The Creator",
                "rationale": "Fits teams that invent cohesive experiences (dummy).",
                "personality_traits": ["Inventive", "Hands-on", "Clear"],
            }
        ],
    }


def _branding_phase2_narrative_with_tagline() -> Dict[str, Any]:
    """Return the Phase 2 narrative-with-archetype payload plus tagline fields.

    Preconditions:
        None.
    Postconditions:
        Returns a fresh dict extending
        ``_branding_phase2_narrative_with_archetype()`` with ``tagline``,
        ``tagline_rationale``, and ``elevator_pitches``.
    """
    return {
        **_branding_phase2_narrative_with_archetype(),
        "tagline": "Ship brand with the product",
        "tagline_rationale": "Ties cohesion to shipping speed (dummy).",
        "elevator_pitches": [
            {"tier": "5-second", "pitch": "On-brand experiences, shipped weekly (dummy)."},
            {
                "tier": "30-second",
                "pitch": (
                    "Dummy Co. helps product teams keep every touchpoint intentional "
                    "without slowing delivery (dummy)."
                ),
            },
            {
                "tier": "2-minute",
                "pitch": (
                    "We turn brand strategy into a workable system so marketing, product, "
                    "and design stay aligned as you ship (dummy)."
                ),
            },
        ],
    }


def _branding_phase2_narrative_with_messaging() -> Dict[str, Any]:
    """Return the Phase 2 narrative-with-tagline payload plus messaging fields.

    Preconditions:
        None.
    Postconditions:
        Returns a fresh dict extending
        ``_branding_phase2_narrative_with_tagline()`` with
        ``messaging_framework`` and ``audience_message_maps``.
    """
    return {
        **_branding_phase2_narrative_with_tagline(),
        "messaging_framework": [
            {
                "pillar": "Cohesion",
                "key_message": "Every touchpoint feels intentional (dummy).",
                "proof_points": ["Shared tokens", "Review gates"],
            },
            {
                "pillar": "Speed",
                "key_message": "Brand work ships with the product (dummy).",
                "proof_points": ["Weekly cadence", "Embedded strategists"],
            },
            {
                "pillar": "Clarity",
                "key_message": "Plain language over jargon (dummy).",
                "proof_points": ["Short docs", "Approved phrases"],
            },
        ],
        "audience_message_maps": [
            {
                "audience_segment": "Enterprise product leaders",
                "primary_message": "Ship cohesive experiences faster (dummy).",
                "supporting_messages": ["Reduce rework", "Align teams"],
                "tone_adjustments": "Confident and concrete",
            }
        ],
    }


def _branding_phase2_narrative_with_personas() -> Dict[str, Any]:
    """Return the Phase 2 narrative-with-messaging payload plus persona profiles.

    Preconditions:
        None.
    Postconditions:
        Returns a fresh dict extending
        ``_branding_phase2_narrative_with_messaging()`` with
        ``persona_profiles``.
    """
    return {
        **_branding_phase2_narrative_with_messaging(),
        "persona_profiles": [
            {
                "name": "Alex Rivera",
                "role": "VP Product",
                "demographics": "Enterprise B2B, 10+ years experience (dummy).",
                "psychographics": "Values clarity and speed (dummy).",
                "goals": ["Ship cohesive UX", "Cut brand rework"],
                "frustrations": ["Inconsistent messaging", "Slow agencies"],
                "media_habits": ["Product communities", "LinkedIn"],
                "jobs_to_be_done": ["Align brand and product delivery"],
            },
            {
                "name": "Jordan Lee",
                "role": "Brand Lead",
                "demographics": "Mid-market org, design-background (dummy).",
                "psychographics": "Protects voice without blocking shipping (dummy).",
                "goals": ["Keep voice consistent", "Enable product teams"],
                "frustrations": ["Ad-hoc copy", "No system of record"],
                "media_habits": ["Design newsletters", "Team wikis"],
                "jobs_to_be_done": ["Codify writing guidelines"],
            },
        ],
    }


def _branding_phase2_narrative_with_writing_guidelines() -> Dict[str, Any]:
    """Return the Phase 2 narrative-with-personas payload plus writing guidelines.

    Preconditions:
        None.
    Postconditions:
        Returns a fresh dict extending
        ``_branding_phase2_narrative_with_personas()`` with a nested
        ``writing_guidelines`` object.
    """
    return {
        **_branding_phase2_narrative_with_personas(),
        "writing_guidelines": {
            "voice_principles": [
                "Use a confident, human voice (dummy).",
                "Prefer concrete proof over slogans (dummy).",
                "Keep sentences short enough to scan (dummy).",
            ],
            "style_dos": [
                "Lead with the customer outcome (dummy).",
                "Use active voice (dummy).",
                "Name the audience when it clarifies (dummy).",
            ],
            "style_donts": [
                "Avoid empty superlatives (dummy).",
                "Don't bury the offer (dummy).",
                "Don't mix casual slang with legal claims (dummy).",
            ],
            "editorial_quality_bar": [
                "Every piece states who it is for (dummy).",
                "Claims cite a proof point (dummy).",
                "Copy matches the approved tone spectrum (dummy).",
            ],
        },
    }


def _branding_structured_stub(system_lowered: str) -> Optional[Dict[str, Any]]:
    """Try Phase 3, then Phase 4, then Phase 5, branding structured-output stubs;
    first match wins.

    Preconditions:
        ``system_lowered`` is the agent system prompt already lowercased (may be empty).
    Postconditions:
        Returns the first non-``None`` result from
        ``_branding_phase3_structured_stub`` / ``_branding_phase4_structured_stub`` /
        ``_branding_phase5_structured_stub``, or ``None`` when none match. Kept as
        its own helper (rather than inlined in ``complete_json``) so that call
        site's branching stays unchanged and under the mccabe complexity ceiling.
    """
    stub = _branding_phase3_structured_stub(system_lowered)
    if stub is not None:
        return stub
    stub = _branding_phase4_structured_stub(system_lowered)
    if stub is not None:
        return stub
    return _branding_phase5_structured_stub(system_lowered)


# Single source of truth for the six class names
# _branding_phase2_structured_output_stub recognizes. Kept separate from that
# function's if-chain (rather than driving the if-chain off this set) so the
# existing, already-tested dispatch body stays untouched; this set exists for
# callers — currently just _looks_like_structured_output_tool — that only
# need a cheap membership check, not the constructed payload.
_PHASE2_STRUCTURED_OUTPUT_MODEL_NAMES: frozenset[str] = frozenset(
    {
        "BrandStoryOutput",
        "BrandArchetypesOutput",
        "TaglineOutput",
        "MessagingFrameworkOutput",
        "PersonaProfilesOutput",
        "WritingGuidelinesOutput",
    }
)


def _branding_phase2_structured_output_stub(model_name: str) -> Optional[Dict[str, Any]]:
    """Deterministic Branding Phase 2 "Narrative & Messaging" payload for a known
    ``structured_output`` model class name.

    This is the routing counterpart to the ``system_lowered`` text anchors the
    six Phase 2 ``elif`` branches in ``complete_json`` use: those branches
    delegate to the exact same functions below via a hardcoded class name, so
    there is one payload per class regardless of which path reaches it. Takes
    a name string rather than the class object itself because two of this
    dispatcher's three callers (``chat()``'s and ``stream()``'s tool-call
    detection) only ever see the Strands tool's ``name`` — which Strands sets
    to ``model.__name__`` — never the Python class; ``complete_json`` derives
    the same string from its own ``structured_output_model`` class parameter
    so all three callers share one dispatch table. The recognized names are
    also listed in ``_PHASE2_STRUCTURED_OUTPUT_MODEL_NAMES``, for callers that
    need the name set without the payload.

    Preconditions:
        ``model_name`` is a string, typically a ``type.__name__``.
    Postconditions:
        Returns the fresh stub dict that the named model class should
        validate against, or ``None`` for any unrecognized name so callers
        can fall back to prompt-text matching.
    """
    if model_name == "BrandStoryOutput":
        return _branding_phase2_narrative_base()
    if model_name == "BrandArchetypesOutput":
        return _branding_phase2_narrative_with_archetype()
    if model_name == "TaglineOutput":
        return _branding_phase2_narrative_with_tagline()
    if model_name == "MessagingFrameworkOutput":
        return _branding_phase2_narrative_with_messaging()
    if model_name == "PersonaProfilesOutput":
        return _branding_phase2_narrative_with_personas()
    if model_name == "WritingGuidelinesOutput":
        return _branding_phase2_narrative_with_writing_guidelines()
    return None


def _looks_like_structured_output_tool(name: str, description_lowered: str) -> bool:
    """True when a tool's name/description indicates Strands' StructuredOutputTool.

    Shared by ``DummyLLMClient.chat()`` and ``.stream()``, whose tool-list
    shapes differ (OpenAI-style ``{"function": {...}}`` vs. flat ``ToolSpec``
    dicts) and so unwrap ``name``/``description`` differently before calling
    this — only the matching criteria itself is shared here.

    Preconditions:
        ``name`` and ``description_lowered`` are strings (may be empty);
        ``description_lowered`` is already lowercased by the caller.
    Postconditions:
        Returns ``True`` if the stable-name check, either description
        substring heuristic, or ``name`` is one of the six known Phase 2
        classes — the last arm matches Strands' actual invariant (it names
        the tool after the model's ``__name__``) independent of description
        wording a future SDK version could change. Checks membership rather
        than calling ``_branding_phase2_structured_output_stub`` so a pure
        boolean check doesn't also construct (and discard) a payload dict.
    """
    return (
        name == "structured_output"
        or "structuredoutputtool" in description_lowered
        or "structured_output" in description_lowered
        or name in _PHASE2_STRUCTURED_OUTPUT_MODEL_NAMES
    )


def _branding_phase2_text_routed_stub(system_lowered: str) -> Optional[Dict[str, Any]]:
    """Text-anchor fallback for the Phase 2 "Narrative & Messaging" cluster.

    Used by ``complete_json`` when the caller didn't supply a recognized
    ``structured_output_model`` (see ``_branding_phase2_structured_output_stub``).
    Kept as its own helper — like ``_branding_structured_stub`` for Phase 3/4 —
    so that call site's branching stays flat and under the mccabe complexity
    ceiling.

    Preconditions:
        ``system_lowered`` is the agent system prompt already lowercased (may be empty).
    Postconditions:
        Returns the first matching Phase 2 payload, or ``None`` when no
        anchor combination matches. Cumulative carry-forward stubs: each
        specialist repeats upstream fields so a linear Graph predecessor
        exposes the full prior narrative — anchors are ordered least- to
        most-specific; earlier branches carry negative guards so a later
        specialist's prompt (which also contains earlier fields) does not
        get caught by an earlier branch.
    """
    if (
        "brand_story" in system_lowered
        and "boilerplate_variants" in system_lowered
        and (
            "tagline_rationale" not in system_lowered
            and "personality_traits" not in system_lowered
            and "messaging_framework" not in system_lowered
            and "jobs_to_be_done" not in system_lowered
            and "writing_guidelines" not in system_lowered
        )
    ):
        return _branding_phase2_narrative_base()
    if "personality_traits" in system_lowered and "carry forward brand_story" in system_lowered:
        return _branding_phase2_narrative_with_archetype()
    if "tagline_rationale" in system_lowered and "elevator_pitches" in system_lowered:
        return _branding_phase2_narrative_with_tagline()
    if "messaging_framework" in system_lowered and "audience_message_maps" in system_lowered:
        return _branding_phase2_narrative_with_messaging()
    if "jobs_to_be_done" in system_lowered and "media_habits" in system_lowered:
        return _branding_phase2_narrative_with_personas()
    if "writing_guidelines" in system_lowered and "editorial_quality_bar" in system_lowered:
        return _branding_phase2_narrative_with_writing_guidelines()
    return None


class DummyLLMClient(LLMClient):
    """No-op implementation for tests and environments without an LLM.

    Also provides the Strands ``Model`` method surface. Importing or using this
    class as a plain :class:`~llm_service.interface.LLMClient` never loads
    ``strands``. Concrete Agent-facing Model members (``stateful``, etc.) are
    defined here so ``strands.Agent(model=DummyLLMClient())`` works without
    relying on virtual ABC registration. Call
    :func:`ensure_strands_model_registration` (or construct after ``strands`` is
    already imported) when ``isinstance(..., Model)`` / MRO inheritance is needed.
    """

    _call_counter: int = 0

    def __init__(self) -> None:
        """Construct a dummy client.

        Preconditions: none.
        Postconditions: request counters/config are initialized; if ``strands`` is
            already present in ``sys.modules``, Strands ``Model`` is attached to
            the class MRO (see :func:`ensure_strands_model_registration`).
        """
        self._request_count = 0
        self._model_config: dict[str, Any] = {}
        # When Strands is already loaded (typical in agent/test processes),
        # attach real Model inheritance immediately so resolve_strands_model's
        # ``isinstance(..., Model)`` short-circuit matches pre-lazy behaviour.
        # Cold LLMClient paths leave strands unloaded and skip this.
        if _strands_already_imported():
            ensure_strands_model_registration()

    # -----------------------------------------------------------------------
    # strands.models.model.Model concrete members (no strands import required)
    # -----------------------------------------------------------------------

    @property
    def stateful(self) -> bool:
        """Whether the model manages conversation state server-side.

        Preconditions: none.
        Postconditions: returns False (matches Strands ``Model`` default). Defined
            here so ``Agent(model=...)`` can read ``model.stateful`` at construction
            without requiring Strands inheritance yet.
        """
        return False

    @property
    def context_window_limit(self) -> int | None:
        """Maximum context window size in tokens from local model config.

        Preconditions: none.
        Postconditions: returns ``context_window_limit`` from ``_model_config`` when
            set, else None (matches Strands ``Model.context_window_limit`` semantics).
        """
        config = self._model_config
        return config.get("context_window_limit") if isinstance(config, dict) else None

    async def count_tokens(
        self,
        messages: list[StrandsMessage],
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        system_prompt_content: list[SystemContentBlock] | None = None,
    ) -> int:
        """Estimate input tokens via Strands ``Model.count_tokens`` once Model is attached.

        Preconditions: ``strands`` is importable.
        Postconditions: returns the Model default estimate; attaches Model to the
            DummyLLMClient MRO if not already attached.
        """
        ensure_strands_model_registration()
        from strands.models.model import Model  # noqa: PLC0415 - intentional lazy import

        return await Model.count_tokens(
            self,
            messages,
            tool_specs=tool_specs,
            system_prompt=system_prompt,
            system_prompt_content=system_prompt_content,
        )

    # -----------------------------------------------------------------------
    # strands.models.model.Model ABC surface (lazy real inheritance)
    # -----------------------------------------------------------------------

    def update_config(self, **model_config: Any) -> None:
        """Update Strands model config.

        Preconditions: none.
        Postconditions: ``model_config`` keys are merged into the local config dict;
            Strands ``Model`` is attached to the class MRO when importable.
        """
        ensure_strands_model_registration()
        self._model_config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        """Return a copy of the Strands model config.

        Preconditions: none.
        Postconditions: returned dict is a shallow copy of the local config;
            Strands ``Model`` is attached to the class MRO when importable.
        """
        ensure_strands_model_registration()
        return dict(self._model_config)

    async def structured_output(
        self,
        output_model: type,
        prompt: list,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yield Strands structured-output events from the dummy pattern matcher.

        Aggregates all user/tool turns (not just the latest) so a follow-up
        like "return that as structured output" still routes on anchors from
        the original request — matching ``LLMClientModel.structured_output``.
        Also forwards ``output_model`` itself as ``structured_output_model``,
        so ``complete_json`` can route by class identity instead of relying
        solely on those text anchors.

        Note: this method is only reached via Strands' deprecated
        ``Agent.structured_output()``/``structured_output_async()`` (or a
        direct caller of this method, as in this repo's tests) — nothing in
        this repo calls those. Agents built with ``structured_output_model=``
        (the current API, what ``build_agent`` uses) are driven through the
        normal event loop instead, which calls ``chat()``/``stream()`` with a
        ``StructuredOutputTool`` in ``tools``/``tool_specs`` — see the
        matching deterministic-routing logic there, which is what branding
        traffic actually exercises.

        Preconditions:
            - ``output_model`` is a Pydantic model type with ``model_validate``.
            - ``prompt`` is a Strands-style message list.

        Postconditions:
            - Yields a single event ``{"output": validated}`` on success.
            - Raises ``TypeError`` when ``output_model`` is not a Pydantic
              model with ``model_validate``.
            - Raises ``ValueError`` when the stub dict cannot validate.
            - Attaches Strands ``Model`` to the class MRO when importable.
        """
        ensure_strands_model_registration()
        if not hasattr(output_model, "model_validate"):
            raise TypeError(
                f"DummyLLMClient.structured_output: output_model {output_model!r} "
                "must be a Pydantic model with a model_validate class method"
            )
        prompt_text = _aggregated_user_tool_text(prompt)
        data = self.complete_json(
            prompt_text, system_prompt=system_prompt, structured_output_model=output_model
        )
        try:
            validated = output_model.model_validate(data)
        except Exception as exc:
            raise ValueError(
                f"DummyLLMClient.structured_output: failed to parse into "
                f"{getattr(output_model, '__name__', output_model)}: {exc}"
            ) from exc
        yield {"output": validated}

    async def stream(
        self,
        messages: list[StrandsMessage],
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        system_prompt_content: list[SystemContentBlock] | None = None,
        invocation_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        """Yield a minimal stream that the Strands Agent event loop can process.

        When ``tool_specs`` contains a StructuredOutputTool (added by Strands
        when ``structured_output_model=...`` is used), yields a tool-use event
        invoking that tool with data from
        ``_branding_phase2_structured_output_stub`` when the tool's name
        resolves to a known Phase 2 model class, falling back to the
        ``complete_json`` pattern matcher otherwise (mirrors ``chat()``, which
        is the method Strands actually calls for branding traffic via
        ``LLMClientModel``; this native ``stream()`` matters for a bare
        ``Agent(model=DummyLLMClient())``). Otherwise yields a plain text
        response.

        When ``system_prompt_content`` is supplied (including an empty list), it
        is treated as authoritative over the legacy ``system_prompt`` string so
        branding Phase 1 branches that anchor on system text still match even
        if a stale string is also present, and an explicit empty override clears
        that stale string.

        Preconditions: ``messages`` is a sequence of Strands-shaped message dicts.
        Postconditions: yields a complete assistant stream; attaches Strands
            ``Model`` to the class MRO when importable; increments
            ``self._request_count`` exactly once per call, mirroring ``chat()``.

        ``**kwargs`` is accepted for ABC compatibility with Strands' ``Model``
        interface and is otherwise ignored by this dummy implementation.
        """
        ensure_strands_model_registration()
        self._request_count += 1
        del tool_choice, invocation_state  # accepted for ABC compatibility
        user_text = _last_user_text(messages)
        if system_prompt_content is not None:
            system_prompt = _flatten_system_prompt_content(system_prompt_content)

        # See _looks_like_structured_output_tool for the shared detection
        # criteria (also used by chat()).
        structured_tool_name = None
        if tool_specs:
            for spec in tool_specs:
                name = spec.get("name", "") or ""
                desc = (spec.get("description") or "").lower()
                if _looks_like_structured_output_tool(name, desc):
                    structured_tool_name = name or "structured_output"
                    break

        # structured_tool_name is exactly the Pydantic model's __name__ when
        # Strands set it (see chat()'s identical check), so a recognized name
        # routes deterministically. Must be resolved before falling back to
        # complete_json's text-anchor scan, not after — computing the two in
        # the other order would compute rich-response data that ignores the
        # tool identity entirely.
        deterministic = (
            _branding_phase2_structured_output_stub(structured_tool_name)
            if structured_tool_name
            else None
        )
        response_data = (
            deterministic
            if deterministic is not None
            else self.complete_json(user_text, system_prompt=system_prompt)
        )
        response_text = (
            json.dumps(response_data) if isinstance(response_data, dict) else str(response_data)
        )

        yield {"messageStart": {"role": "assistant"}}

        if structured_tool_name:
            # Yield a tool-use block so Strands' structured output flow works
            tool_use_id = f"dummy_tool_{structured_tool_name}"
            yield {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {
                        "toolUse": {"toolUseId": tool_use_id, "name": structured_tool_name},
                    },
                },
            }
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"toolUse": {"input": response_text}},
                },
            }
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
        else:
            yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
            yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": response_text}}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}

        yield {
            "messageStop": {"stopReason": "tool_use" if structured_tool_name else "end_turn"},
            "metadata": {
                "usage": {
                    "inputTokens": len(user_text) // 4,
                    "outputTokens": len(response_text) // 4,
                    "totalTokens": (len(user_text) + len(response_text)) // 4,
                },
                "metrics": {"latencyMs": 1},
            },
        }

    @property
    def request_count(self) -> int:
        """Total number of LLM requests (for compatibility with blog tests)."""
        return self._request_count

    @staticmethod
    def _extract_task_hint(prompt: str) -> str:
        for line in prompt.split("\n"):
            stripped = line.strip()
            if stripped.startswith("**Task:**"):
                return stripped.replace("**Task:**", "").strip()
        return hashlib.md5(prompt.encode(), usedforsecurity=False).hexdigest()[:12]

    def get_max_context_tokens(self) -> int:
        return 16384

    def complete(
        self,
        prompt: str,
        *,
        objective: str = "dummy",
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
    ) -> str:
        # ``objective`` is accepted to match the LLMClient contract; the dummy
        # client makes no real LLM call and performs no attribution, so it
        # tolerates an omitted objective (test stubs need not declare one).
        self._request_count += 1
        return "Dummy text completion (no LLM)."

    def complete_json(
        self,
        prompt: str,
        *,
        objective: str = "dummy",
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
        structured_output_model: Optional[type] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Return a JSON-shaped stub for the given prompt.

        Routes the Branding Phase 2 "Narrative & Messaging" cluster
        deterministically by ``structured_output_model``'s class name when
        provided (see ``_branding_phase2_structured_output_stub``); otherwise
        — and for every other prompt shape this dummy stubs — pattern-matches
        against ``prompt`` (and, for a few teams, ``system_prompt``) for
        anchor tokens and returns the matching canned dict.

        Preconditions:
            - ``prompt`` is a string (may be empty).
            - ``structured_output_model``, if given, is the exact
              ``structured_output=``/``structured_output_model=`` class the
              caller's agent was built with.

        Postconditions:
            - Returns a dict. Every recognized shape validates against its
              corresponding Pydantic model; an unrecognized prompt returns
              the generic ``{"output": ..., "status": "ok"}`` fallback.
            - Increments ``self._request_count`` and the shared
              ``DummyLLMClient._call_counter`` exactly once per call.
        """
        # ``objective`` keeps a default here (unlike the required LLMClient
        # contract) on purpose: the dummy records no telemetry, and forcing every
        # test stub call to pass an objective adds churn with no attribution value.
        # Production enforcement lives in the real OllamaLLMClient (required, and
        # rejects empty strings).
        # Pattern-match mostly against the user prompt. Callers (including
        # Strands-migrated agents that hand their persona to the Strands
        # ``Agent`` as a system prompt) must include the anchor tokens the
        # branches below look for in the user prompt they build. Scanning
        # ``system_prompt`` with loose single-word anchors (``"pipeline"``,
        # ``"security"``) was tried and reverted because it cross-contaminated
        # other teams' prompts that happened to mention those words in persona
        # text.
        #
        # Branding Phase 1 / Phase 3 branches are the exception: they anchor
        # on ``system_prompt`` (via ``system_lowered`` later in this method)
        # because every agent in those phases receives the same serialized
        # mission/phase context as its user message, so only each agent's own
        # system_prompt (its required output field names) can distinguish
        # which one is asking. Those anchors are multi-token combinations
        # unique to one agent's prompt. Phase 2 used the same system_prompt
        # anchoring historically, but is now routed deterministically by the
        # structured_output_model's class name below when a caller supplies
        # it — see the fast path immediately after this comment block; the
        # system_prompt anchors in ``_branding_phase2_text_routed_stub``
        # remain only as a fallback for callers that don't.
        lowered = prompt.lower()
        # Shared across instances so sequential coding stubs can mint distinct
        # module/component names when the task hint alone is not enough.
        DummyLLMClient._call_counter += 1
        self._request_count += 1
        counter = DummyLLMClient._call_counter
        task_hint = self._extract_task_hint(prompt)

        # Deterministic fast path: when the caller hands us the actual
        # structured_output model class (Strands' bridge does, via
        # LLMClientModel.structured_output/chat/stream), route by its class
        # *name* instead of the text-anchor scan below — sidesteps the exact
        # fragility this scan is prone to (a prompt reword or an
        # incidentally-mentioned field name silently returning the wrong
        # shape). A name string, not the class object itself, because the
        # production stream()/chat() path only ever has the tool's name
        # available (Strands sets it to the class's __name__), never the
        # Python class — see _branding_phase2_structured_output_stub's own
        # docstring. Only a known subset of classes are wired up; anything
        # else falls through unchanged.
        if structured_output_model is not None:
            deterministic = _branding_phase2_structured_output_stub(
                structured_output_model.__name__
            )
            if deterministic is not None:
                return deterministic

        if "architecture_document" in lowered and "components" in lowered and "overview" in lowered:
            return {
                "overview": "API backend + WebApp frontend (Dummy architecture).",
                "architecture_document": "# System Architecture (Dummy)\n\nPlaceholder architecture.",
                "components": [
                    {"name": "API", "type": "backend"},
                    {"name": "WebApp", "type": "frontend"},
                ],
                "diagrams": {
                    "client_server_architecture": "graph LR\n  Browser-->API\n  API-->DB",
                    "frontend_code_structure": "graph TD\n  App-->Components\n  App-->Services",
                },
                "decisions": [
                    {
                        "decision": "Use REST API",
                        "context": "Standard web stack",
                        "consequences": "Simple integration",
                    }
                ],
            }
        elif "codebase audit" in lowered and "files_inventory" in lowered:
            return {
                "files_inventory": [
                    {
                        "path": "initial_spec.md",
                        "language": "markdown",
                        "purpose": "Project specification",
                        "key_exports": [],
                    }
                ],
                "frameworks": {
                    "backend": "unknown",
                    "frontend": "unknown",
                    "database": "unknown",
                    "testing": "unknown",
                    "cicd": "unknown",
                    "other": [],
                },
                "existing_functionality": ["Project specification document exists"],
                "partial_implementations": [],
                "gaps": [
                    "No application code exists yet",
                    "No backend framework set up",
                    "No frontend framework set up",
                    "No CI/CD pipeline",
                    "No database configuration",
                    "No tests",
                ],
                "code_conventions": {
                    "naming": "unknown",
                    "structure": "flat",
                    "config_approach": "unknown",
                },
                "summary": "The repository contains only the project specification (initial_spec.md). No application code, infrastructure, or tests exist yet. The entire application needs to be built from scratch according to the spec.",
            }
        elif "deep analysis" in lowered and "total_deliverable_count" in lowered:
            return {
                "data_entities": [
                    {
                        "name": "User",
                        "attributes": ["id", "email", "password_hash", "created_at"],
                        "relationships": [],
                        "validation_rules": ["email must be valid", "password required"],
                    }
                ],
                "api_endpoints": [
                    {
                        "method": "POST",
                        "path": "/auth/signup",
                        "description": "Create new user account",
                        "auth_required": False,
                    },
                    {
                        "method": "POST",
                        "path": "/auth/login",
                        "description": "Authenticate user and return JWT",
                        "auth_required": False,
                    },
                    {
                        "method": "POST",
                        "path": "/auth/refresh",
                        "description": "Refresh access token",
                        "auth_required": True,
                    },
                    {
                        "method": "GET",
                        "path": "/api/users/me",
                        "description": "Get current user profile",
                        "auth_required": True,
                    },
                ],
                "ui_screens": [
                    {
                        "name": "Login Page",
                        "description": "User login form",
                        "components": ["LoginForm", "ErrorDisplay"],
                        "states": ["idle", "loading", "error", "success"],
                    },
                    {
                        "name": "Registration Page",
                        "description": "User registration form",
                        "components": ["RegistrationForm", "ErrorDisplay"],
                        "states": ["idle", "loading", "error", "success"],
                    },
                    {
                        "name": "Dashboard",
                        "description": "Main authenticated view",
                        "components": ["Navbar", "UserProfile"],
                        "states": ["loading", "loaded"],
                    },
                ],
                "user_flows": [
                    {
                        "name": "User Registration",
                        "steps": [
                            "Navigate to signup",
                            "Fill form",
                            "Submit",
                            "Receive confirmation",
                            "Redirect to login",
                        ],
                    },
                    {
                        "name": "User Login",
                        "steps": [
                            "Navigate to login",
                            "Enter credentials",
                            "Submit",
                            "Receive JWT",
                            "Redirect to dashboard",
                        ],
                    },
                ],
                "non_functional": [
                    {"category": "security", "requirement": "Passwords must be hashed with bcrypt"},
                    {"category": "security", "requirement": "JWT tokens must expire"},
                    {"category": "performance", "requirement": "API response time under 500ms"},
                ],
                "infrastructure": [
                    {"category": "deployment", "requirement": "Docker containerization"},
                    {"category": "cicd", "requirement": "Automated CI/CD pipeline"},
                ],
                "integrations": [],
                "total_deliverable_count": 18,
                "summary": "The spec requires a full-stack authentication application with user registration, login, token refresh, and protected routes. The backend needs FastAPI with JWT auth, the frontend needs Angular with login/registration/dashboard screens, and DevOps needs Docker and CI/CD.",
            }
        elif "qa agent has reviewed code" in lowered and "fix tasks" in lowered:
            return {"tasks": [], "rationale": "QA approved; no fix tasks needed (dummy)."}
        elif "run security review now" in lowered and "90%" in lowered:
            return {"run_security": False, "rationale": "Code coverage not yet at 90% (dummy)."}
        elif "reviewing the progress" in lowered and "spec_compliance_pct" in lowered:
            return {
                "tasks": [],
                "spec_compliance_pct": 50,
                "gaps_identified": [],
                "rationale": "Progress review complete. Current tasks cover the planned scope (dummy).",
            }
        elif "clarification questions from specialist" in lowered:
            return {
                "title": "Refined Task Title",
                "description": "Refined task description with additional details from spec. The implementation should follow Angular best practices using standalone components and reactive forms. All public methods must have JSDoc documentation. Error states must be handled with user-friendly messages.",
                "user_story": "As a user, I want refined functionality so that the feature works as specified in the requirements.",
                "requirements": "Detailed requirements addressing clarification questions. Use Angular Material for UI components. Implement loading spinners during async operations. Handle HTTP errors with retry logic.",
                "acceptance_criteria": [
                    "Criterion 1: Component renders without errors",
                    "Criterion 2: User interactions trigger correct API calls",
                    "Criterion 3: Error states display meaningful messages",
                ],
            }
        elif (
            ("execution_order" in lowered or "task_assignments" in lowered) and "tasks" in lowered
        ) or (
            # Strands-migrated Tech Lead: user prompt has product context
            # while execution_order / initiative → epic → story keywords
            # live in the system prompt.
            system_prompt
            and "execution_order" in system_prompt.lower()
            and "initiative" in system_prompt.lower()
            and "**product title:**" in lowered
        ):
            return {
                "tasks": [
                    {
                        "id": "git-setup",
                        "title": "Initialize Git Development Branch",
                        "type": "git_setup",
                        "description": "Ensure the development branch exists.",
                        "user_story": "As a developer, I want a dedicated development branch.",
                        "assignee": "devops",
                        "requirements": "Create development branch from main if missing.",
                        "acceptance_criteria": ["Development branch exists and is checked out"],
                        "dependencies": [],
                    },
                    {
                        "id": "devops-dockerfile",
                        "title": "Multi-Stage Dockerfile",
                        "type": "devops",
                        "description": "Create a multi-stage Dockerfile.",
                        "user_story": "As a developer, I want a multi-stage Dockerfile.",
                        "assignee": "devops",
                        "requirements": "Multi-stage Dockerfile.",
                        "acceptance_criteria": ["Dockerfile builds successfully"],
                        "dependencies": ["git-setup"],
                    },
                ],
                "execution_order": ["git-setup", "devops-dockerfile"],
                "rationale": "Granular plan (dummy).",
                "summary": "2 tasks (dummy).",
                "requirement_task_mapping": [],
                "clarification_questions": [],
            }
        elif (
            "acceptance_trace" in lowered
            and "quality_gates" in lowered
            and "validation_evidence" in lowered
            and "acceptance criteria" in lowered
        ):
            # QA acceptance_evidence mode (absorbs the former DevOps test
            # validation surface). Kept ABOVE the ``bugs_found`` QA branch
            # because this prompt maps evidence to criteria rather than
            # reviewing code. The four anchor tokens (``acceptance_trace`` +
            # ``quality_gates`` + ``validation_evidence`` from the output schema,
            # plus the literal "acceptance criteria" from the instruction) make a
            # false match against another team's prompt effectively impossible.
            return {
                "approved": True,
                "quality_gates": {"unit_tests": "pass", "integration_tests": "pass"},
                "acceptance_trace": [
                    {"criterion": "Criterion 1", "implementation_refs": [], "tests": []}
                ],
                "validation_evidence": [
                    {"gate": "unit_tests", "status": "pass", "detail": "Dummy evidence"}
                ],
                "bugs_found": [],
                "summary": "Dummy acceptance evidence",
            }
        elif "bugs_found" in lowered and (
            "integration_test" in lowered or "readme_content" in lowered or "test_plan" in lowered
        ):
            # Kept ABOVE code-review catch-all and security/accessibility
            # branches because QA prompts now include a shared
            # REVIEW_PRIORITY_FRAMEWORK that mentions "security
            # vulnerabilities", and QA user prompts also contain
            # "code to review" which would match the code-review catch-all.
            # ``bugs_found`` is the anchor token — it's unique to the QA
            # output contract.
            return {
                "bugs_found": [],
                "integration_tests": "# Dummy integration test",
                "unit_tests": "# Dummy unit tests",
                "test_plan": "Dummy test plan",
                "summary": "Dummy QA assessment",
                "live_test_notes": "Dummy notes",
                "readme_content": "# Dummy README",
                "suggested_commit_message": "test: add integration tests",
                "approved": True,
            }
        elif "senior code reviewer" in lowered and ("approved" in lowered or "issues" in lowered):
            return {
                "approved": True,
                "issues": [],
                "summary": "Code review passed (dummy).",
                "spec_compliance_notes": "Code aligns with task requirements.",
            }
        elif "security" in lowered and "vulnerabilities" in lowered:
            # Kept ABOVE the code-review catch-all because the security agent's
            # own prompt includes "Code to review" as a section header, which
            # would otherwise match the catch-all first and return an empty
            # generic review instead of the vulnerabilities-shaped stub.
            return {"vulnerabilities": [], "summary": "No security issues found (dummy)"}
        elif "accessibility" in lowered and "wcag" in lowered and "issues" in lowered:
            # Kept ABOVE the code-review catch-all for the same reason as the
            # security branch above: the accessibility agent's prompt also
            # includes "Code to review" as a section header.
            return {"issues": [], "summary": "No WCAG 2.2 accessibility issues found (dummy)"}
        elif (
            "code to review" in lowered
            or "review this code" in lowered
            or ("chunk" in lowered and "review" in lowered)
        ) and ("approved" not in lowered or len(lowered) > CODE_REVIEW_MIN_PROMPT_LENGTH):
            # Catch-all for code review / chunk review prompts routed through Strands.
            # Long prompts that mention "approved" still match: they carry full review
            # context, unlike short approval-only stubs below the length threshold.
            # "chunk" alone is too broad (matches unrelated data-processing prompts);
            # real chunk-review prompts always pair it with "review" (see
            # CHUNK_REVIEW_NOTE / CODE_TO_REVIEW_HEADER in chunk_reviewer.py).
            return {
                "approved": True,
                "issues": [],
                "summary": "Code review passed (dummy).",
                "spec_compliance_notes": "",
            }
        elif "senior backend software engineer" in lowered:
            slug = (
                _extract_name_from_hint(task_hint, separator="_", max_length=25)
                or f"module_{counter}"
            )
            # task_hint is free-form prompt text and may contain quote characters
            # or "\"\"\"" sequences; keep the docstring static and put the hint in
            # a repr()-encoded comment so the generated source stays valid Python
            # regardless of what it contains.
            return {
                "code": f'"""Backend module."""\n# Task: {task_hint!r}\nfrom fastapi import APIRouter\nrouter = APIRouter()\n',
                "language": "python",
                "summary": f"Backend implementation for: {task_hint}",
                "files": {
                    f"app/routers/{slug}.py": f'"""Backend module."""\n# Task: {task_hint!r}\nfrom fastapi import APIRouter\nrouter = APIRouter()\n',
                    f"tests/test_{slug}.py": f'"""Tests."""\n# Task: {task_hint!r}\ndef test_{slug}():\n    assert True\n',
                },
                "tests": f'"""Tests."""\n# Task: {task_hint!r}\ndef test_{slug}():\n    assert True\n',
                "suggested_commit_message": f"feat(api): implement {slug.replace('_', ' ')}",
            }
        elif "senior frontend software engineer" in lowered:
            slug = (
                _extract_name_from_hint(task_hint, separator="-", max_length=25)
                or f"component-{counter}"
            )
            class_name = "".join(w.capitalize() for w in slug.split("-")) + "Component"
            selector = f"app-{slug}"
            return {
                "code": f"import {{ Component }} from '@angular/core';\n@Component({{ selector: '{selector}', template: '<div>{task_hint}</div>' }})\nexport class {class_name} {{}}\n",
                "summary": f"Frontend component for: {task_hint}",
                "files": {
                    f"src/app/components/{slug}/{slug}.component.ts": f"import {{ Component }} from '@angular/core';\n@Component({{ selector: '{selector}', template: '<div>{task_hint}</div>' }})\nexport class {class_name} {{}}\n",
                    f"src/app/components/{slug}/{slug}.component.spec.ts": f"import {{ {class_name} }} from './{slug}.component';\ndescribe('{class_name}', () => {{ it('should create', () => {{}}); }});\n",
                },
                "components": [class_name],
                "suggested_commit_message": f"feat(ui): add {slug} component",
            }
        elif "devops" in lowered or "pipeline" in lowered:
            return {
                "pipeline_yaml": f"# CI Pipeline (task #{counter})\nname: ci\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n",
                "iac_content": f"# Infrastructure (task #{counter})\n",
                "dockerfile": f"# Dockerfile (task #{counter})\nFROM python:3.11-slim\nWORKDIR /app\n",
                "docker_compose": f"# Docker Compose (task #{counter})\nversion: '3.8'\nservices:\n  backend:\n    build: .\n",
                "summary": f"DevOps configuration generated for: {task_hint[:60]}",
                "suggested_commit_message": f"ci: add devops configuration (task #{counter})",
            }
        elif (
            "technical writer" in lowered
            and "readme_content" in lowered
            and "readme_changed" in lowered
        ):
            return {
                "readme_content": f"# Project\n\nAuto-generated documentation (task #{counter}).\n",
                "readme_changed": True,
                "summary": f"Updated README (task #{counter})",
                "suggested_commit_message": f"docs(readme): update (task #{counter})",
            }
        elif "contributors.md" in lowered and "contributors_content" in lowered:
            return {
                "contributors_content": "# Contributors\n| Agent | Role |\n|-------|------|\n",
                "contributors_changed": True,
                "summary": "Updated contributors list (dummy)",
            }
        elif "documentation update needed" in lowered and "should_update_docs" in lowered:
            return {
                "should_update_docs": True,
                "rationale": "Task completed with code changes (dummy).",
            }
        elif (
            "design by contract" in lowered
            and "comments_added" in lowered
            and "already_compliant" in lowered
        ):
            return {
                "files": {},
                "comments_added": 0,
                "comments_updated": 0,
                "already_compliant": True,
                "summary": "All code fully complies with Design by Contract.",
                "suggested_commit_message": "docs(dbc): verify Design by Contract compliance",
            }
        elif "acceptance_criteria" in lowered and "specification" in lowered:
            return {
                "title": "Software Project",
                "description": "Project specification (parsed from initial_spec.md).",
                "acceptance_criteria": ["See specification document"],
                "constraints": [],
                "priority": "medium",
            }
        elif (
            "integration expert" in lowered
            and "backend code" in lowered
            and "frontend code" in lowered
        ):
            return {
                "issues": [],
                "passed": True,
                "summary": "Backend and frontend API contract aligned (dummy).",
                "fix_task_suggestions": [],
            }
        elif "acceptance criteria verifier" in lowered and "per_criterion" in lowered:
            return {
                "per_criterion": [
                    {
                        "criterion": "Criterion 1",
                        "satisfied": True,
                        "evidence": "Code implements the requirement.",
                    }
                ],
                "all_satisfied": True,
                "summary": "All acceptance criteria satisfied (dummy).",
            }
        elif (
            system_prompt
            and "senior software engineer" in system_prompt.lower()
            and "files_to_create_or_edit" in system_prompt.lower()
        ):
            th = self._extract_task_hint(prompt)
            return {
                "summary": f"Implemented (dummy): {th}",
                "files_to_create_or_edit": [
                    {"path": "dummy_impl.txt", "content": f"# dummy implementation for {th}\n"}
                ],
                "commands_run": [],
                "ready_for_review": True,
            }
        # Blogging: plan-critic report (token lives in the user prompt tail)
        elif "plancriticreport" in lowered or "return a single plancriticreport" in lowered:
            return {
                "status": "PASS",
                "approved": True,
                "violations": [],
                "notes": "Dummy plan critic: rubber-stamp PASS for tests.",
                "rubric_version": "v1",
            }
        # Blogging: structured content plan JSON (planning agent; token in user prompt)
        elif "content_plan_json_v1" in lowered:
            return {
                "overarching_topic": "Dummy blog topic",
                "narrative_flow": "Open with context, develop the core idea, close with actions.",
                "sections": [
                    {
                        "title": "Introduction",
                        "coverage_description": "Hook and problem framing.",
                        "order": 0,
                        "research_support_note": "Supported by research digest.",
                        "gap_flag": False,
                    },
                    {
                        "title": "Core ideas",
                        "coverage_description": "Main substance from sources.",
                        "order": 1,
                        "research_support_note": None,
                        "gap_flag": False,
                    },
                    {
                        "title": "Conclusion",
                        "coverage_description": "Recap and one next step.",
                        "order": 2,
                        "research_support_note": None,
                        "gap_flag": False,
                    },
                    {
                        "title": "Further reading",
                        "coverage_description": "Optional pointers (keeps section count in band for standard_article).",
                        "order": 3,
                        "research_support_note": None,
                        "gap_flag": False,
                    },
                ],
                "title_candidates": [
                    {
                        "title": "Dummy Title: Why This Topic Matters",
                        "probability_of_success": 0.72,
                    },
                    {"title": "A Practical Take on the Topic", "probability_of_success": 0.58},
                ],
                "requirements_analysis": {
                    "plan_acceptable": True,
                    "scope_feasible": True,
                    "research_gaps": [],
                    "fits_profile": True,
                    "gaps": [],
                    "risks": [],
                    "suggested_format_change": None,
                },
                "plan_version": 1,
            }
        # Branding team — Phase 1 "Strategic Core" agents (built with
        # structured_output=, see agents.py). Anchored on system_prompt
        # rather than the user prompt: every Phase 1 agent receives the same
        # serialized BrandingMission as its user message, so only the
        # agent-specific system_prompt (each agent's required output field
        # names) can distinguish which one is asking. All six have required
        # fields with no defaults, so the generic fallback below would fail
        # Strands' structured-output validation and exhaust its forced retry.
        system_lowered = (system_prompt or "").lower()
        if (
            "current_brand_perception" in system_lowered
            and "stakeholder_insights" in system_lowered
        ):
            return {
                "current_brand_perception": "Seen as a capable but generic vendor (dummy).",
                "market_position": "Mid-market challenger without a distinct point of view (dummy).",
                "strengths": ["Responsive delivery", "Deep domain expertise"],
                "weaknesses": ["Inconsistent messaging", "Low brand recall"],
                "opportunities": [
                    "Category is consolidating",
                    "Buyers want a clear category leader",
                ],
                "threats": ["Larger competitors out-spending on brand"],
                "stakeholder_insights": [
                    "Sales wants sharper differentiation",
                    "Customers want proof points",
                ],
            }
        elif "brand_purpose" in system_lowered and "vision_statement" in system_lowered:
            return {
                "brand_purpose": "Dummy Co. exists to help teams ship cohesive brand experiences (dummy).",
                "mission_statement": "We turn brand strategy into consistent day-to-day execution (dummy).",
                "vision_statement": "A world where every customer touchpoint feels intentional (dummy).",
            }
        elif "behavioral_definition" in system_lowered and "observable_behaviors" in system_lowered:
            return {
                "core_values": [
                    {
                        "value": "Clarity",
                        "behavioral_definition": "We communicate plainly and avoid jargon (dummy).",
                        "observable_behaviors": ["Write short docs", "Avoid buzzwords"],
                    },
                    {
                        "value": "Trust",
                        "behavioral_definition": "We keep commitments and are transparent (dummy).",
                        "observable_behaviors": [
                            "Disclose tradeoffs",
                            "Follow through on promises",
                        ],
                    },
                    {
                        "value": "Momentum",
                        "behavioral_definition": "We execute with discipline and speed (dummy).",
                        "observable_behaviors": ["Ship in small increments", "Unblock quickly"],
                    },
                ]
            }
        elif "decision_drivers" in system_lowered and "pain_points" in system_lowered:
            return {
                "target_audience_segments": [
                    {
                        "name": "Enterprise product leaders",
                        "description": "Leaders responsible for cohesive digital experiences (dummy).",
                        "pain_points": ["Inconsistent branding", "Slow execution"],
                        "goals": ["Ship cohesive experiences", "Move faster"],
                        "decision_drivers": ["Proven track record", "Clear communication"],
                    }
                ]
            }
        elif "competitive_context" in system_lowered and "proof_points" in system_lowered:
            return {
                "differentiation_pillars": [
                    {
                        "pillar": "Execution speed",
                        "proof_points": ["Ships weekly", "Small dedicated team"],
                        "competitive_context": "Competitors rely on slow agency handoffs (dummy).",
                    },
                    {
                        "pillar": "Hands-on partnership",
                        "proof_points": [
                            "Direct access to strategists",
                            "No account-manager layer",
                        ],
                        "competitive_context": "Competitors route through account managers (dummy).",
                    },
                ]
            }
        elif "positioning_statement" in system_lowered and "brand_promise" in system_lowered:
            return {
                "positioning_statement": (
                    "For enterprise product leaders who need cohesive digital experiences, Dummy Co. "
                    "is the hands-on partner that delivers clarity because execution speed sets us "
                    "apart (dummy)."
                ),
                "brand_promise": "Every customer touchpoint will feel cohesive and intentional (dummy).",
            }
        # Branding team — Phase 2 "Narrative & Messaging" Graph agents (built
        # with structured_output=, see agents.py). Text-anchor fallback lives
        # in _branding_phase2_text_routed_stub so this call site's branching
        # stays flat and under the mccabe complexity ceiling (mirrors
        # _branding_structured_stub for Phase 3/4/5 just below).
        elif (phase2_stub := _branding_phase2_text_routed_stub(system_lowered)) is not None:
            return phase2_stub
        # Branding Phase 3 / Phase 4 / Phase 5 stubs live in ``_branding_structured_stub``
        # so ``complete_json`` stays under the mccabe complexity ceiling. Only
        # ``None`` means "unmatched" — an intentional empty dict must not
        # fall through to the generic default.
        stub = _branding_structured_stub(system_lowered)
        return stub if stub is not None else {"output": "Dummy response", "status": "ok"}

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        objective: str = "dummy",
        response_format: str = "json",
        temperature: float = 0.2,
        tools: Optional[list] = None,
        think: bool | str | None = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> Any:
        """One unified dummy chat round, parameterized by ``response_format``.

        Branches, in order of precedence:

        1. **Strands structured output** (``tools`` contains a
           ``StructuredOutputTool``): return a single tool call invoking it
           with data from ``_branding_phase2_structured_output_stub`` when the
           tool's name resolves to a known Phase 2 model class (Strands names
           the tool after ``output_model.__name__``), falling back to the
           ``complete_json`` pattern matcher otherwise. This is the path real
           ``build_agent(structured_output=...)`` callers actually take —
           Strands drives ``structured_output_model=`` agents through the
           tool-calling event loop, which lands here, not on
           ``structured_output()``.
        2. **Legacy tool loop** (``tools`` provided, no prior tool result):
           emit a no-op ``git_status`` tool call.
        3. **Follow-up rounds or no tools**: run the user prompt through
           ``complete_json``. For ``response_format="json"`` return the dict;
           for ``response_format="text"`` JSON-serialize the dict to a string
           so callers exercising the prose path see deterministic text.
        """
        if response_format not in ("json", "text"):
            raise ValueError(f"response_format must be 'json' or 'text', got {response_format!r}")
        self._request_count += 1
        system_prompt = None
        user_prompt = ""
        for m in messages:
            if not isinstance(m, dict):
                continue
            if m.get("role") == "system":
                system_prompt = m.get("content")
            elif m.get("role") == "user":
                user_prompt = m.get("content") or ""

        has_tool_result = any(isinstance(m, dict) and m.get("role") == "tool" for m in messages)

        if tools and not has_tool_result:
            structured_tool = None
            for t in tools:
                fn = (t or {}).get("function") or {}
                # See _looks_like_structured_output_tool for the shared
                # detection criteria (also used by stream()).
                name = fn.get("name") or ""
                desc = (fn.get("description") or "").lower()
                if _looks_like_structured_output_tool(name, desc):
                    structured_tool = fn
                    break

            if structured_tool is not None:
                # structured_tool["name"] is exactly the Pydantic model's
                # __name__ (Strands names the StructuredOutputTool after the
                # model class), so a recognized name routes deterministically
                # instead of falling through to complete_json's text-anchor
                # scan — this is the actual production call path for
                # structured_output= agents (Strands drives them through the
                # tool-calling loop, not Model.structured_output()), so this
                # check matters more than the one in complete_json itself.
                data = _branding_phase2_structured_output_stub(structured_tool.get("name") or "")
                if data is None:
                    # Produce stub data via the pattern matcher and invoke the
                    # structured output tool with it. Strands will validate the
                    # arguments against the Pydantic schema attached to the tool.
                    data = self.complete_json(
                        user_prompt,
                        temperature=temperature,
                        system_prompt=system_prompt,
                        tools=None,
                        think=think,
                        **kwargs,
                    )
                return {
                    "__tool_calls__": [
                        {
                            "id": f"dummy_{structured_tool.get('name', 'structured')}",
                            "type": "function",
                            "function": {
                                "name": structured_tool.get("name", "structured_output"),
                                "arguments": data,
                            },
                        }
                    ]
                }

            # Legacy path — tests that drive ``complete_json_with_tool_loop``
            # rely on this first-round git_status handoff.
            return {
                "__tool_calls__": [
                    {
                        "id": "dummy_git_status",
                        "type": "function",
                        "function": {"name": "git_status", "arguments": {}},
                    }
                ]
            }

        data = self.complete_json(
            user_prompt,
            temperature=temperature,
            system_prompt=system_prompt,
            tools=None,
            think=think,
            **kwargs,
        )
        if response_format == "text":
            # Preserve the existing dummy text-mode contract: the pattern
            # matcher returns dicts; JSON-serialize them so the assertion
            # shape (``json.loads(text) == {...}``) used across the test
            # suite continues to hold.
            return json.dumps(data) if isinstance(data, dict) else str(data)
        return data
