# Branding Phase 4/5 Model-Class Stub Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route DummyLLMClient Phase 4 and Phase 5 branding stubs by structured-output model class name (mirroring Phase 2), keeping text-anchor fallbacks when no model name is available.

**Architecture:** Extract today's Phase 4/5 inline stub dicts into payload helpers; add `_branding_phase{4,5}_structured_output_stub` dispatchers keyed by Pydantic `__name__`; rename text scanners to `_branding_phase{4,5}_text_routed_stub`; introduce a thin Phase 2→4→5 resolver used by `complete_json` / `chat` / `stream`. For `ChannelGuidelineOutput`, class name selects the stub; `system_lowered` only supplies the `channel` string.

**Tech Stack:** Python 3.10, `DummyLLMClient` in `llm_service.clients.dummy`, pytest + ruff for verification.

**Spec:** `docs/superpowers/specs/2026-08-07-branding-phase4-5-stub-routing-design.md`

## Global Constraints

- Scope is Phase 4 and Phase 5 only; Phase 3 stays text-routed.
- Keep text-anchor fallbacks; do not delete them.
- `ChannelGuidelineOutput`: dispatch by class name; extract `channel` via `re.search(r"channel:\s*'([a-z_]+)'", system_lowered)` with fallback `"channel"`.
- Do not add new regression tests here (sibling issue owns them).
- Touch `backend/agents/llm_service/clients/dummy.py` only for code (plan/spec docs already exist).
- Never reference GitHub issue numbers in code, comments, or commit messages.
- Design by Contract: every new/renamed public helper gets `Preconditions:` / `Postconditions:` docstring sections.
- Work exclusively in `.worktrees/5558-branding-phase4-5-stub-routing` on branch `fix/5558-branding-phase4-5-stub-routing`.
- Run verification from the worktree's `backend/` using the main-repo venv when needed:
  `/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest …`
  with cwd set to the worktree `backend/`.

---

## File map

| File | Responsibility |
|---|---|
| `backend/agents/llm_service/clients/dummy.py` | Phase 4/5 payload helpers, model-name stubs, text-route renames, resolver, call-site wiring, tool-name membership |

No new modules. No test file edits in this plan.

---

### Task 1: Phase 4 payload helpers + model-name stub + text-route rename

**Files:**
- Modify: `backend/agents/llm_service/clients/dummy.py` (replace `_branding_phase4_structured_stub` block ~636–735; place new helpers immediately before the renamed text router)

**Interfaces:**
- Consumes: existing inline Phase 4 stub dicts (move verbatim)
- Produces:
  - `_phase4_channel_value(system_lowered: str) -> str`
  - `_phase4_channel_guide_stub(channel_value: str) -> Dict[str, Any]`
  - `_phase4_experience_principles_stub() -> Dict[str, Any]`
  - `_phase4_architecture_stub() -> Dict[str, Any]`
  - `_phase4_brand_in_action_stub() -> Dict[str, Any]`
  - `_PHASE4_STRUCTURED_OUTPUT_MODEL_NAMES: frozenset[str]`
  - `_branding_phase4_structured_output_stub(model_name: str, system_lowered: str = "") -> Optional[Dict[str, Any]]`
  - `_branding_phase4_text_routed_stub(system_lowered: str) -> Optional[Dict[str, Any]]` (renamed from `_branding_phase4_structured_stub`)

- [ ] **Step 1: Replace `_branding_phase4_structured_stub` with payload helpers, model stub, and text router**

Delete `def _branding_phase4_structured_stub(...)` through its final `return None` and insert:

```python
def _phase4_channel_value(system_lowered: str) -> str:
    """Extract the channel identifier from a lowercased channel-guide prompt.

    Preconditions:
        ``system_lowered`` is already lowercased (may be empty).
    Postconditions:
        Returns the ``[a-z_]+`` group from ``channel: '…'`` when present,
        otherwise ``"channel"``.
    """
    channel_match = re.search(r"channel:\s*'([a-z_]+)'", system_lowered)
    return channel_match.group(1) if channel_match else "channel"


def _phase4_channel_guide_stub(channel_value: str) -> Dict[str, Any]:
    """Return the shared ``ChannelGuidelineOutput`` dummy payload.

    Preconditions:
        ``channel_value`` is a non-empty string (caller supplies the extracted
        or default channel id).
    Postconditions:
        Returns a fresh dict matching the ``ChannelGuidelineOutput`` field set.
    """
    assert isinstance(channel_value, str) and channel_value, (
        "channel_value must be a non-empty string"
    )
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


def _phase4_experience_principles_stub() -> Dict[str, Any]:
    """Return the ``BrandExperiencePrinciplesOutput`` dummy payload.

    Preconditions: none.
    Postconditions: returns a fresh dict matching that schema's field set.
    """
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


def _phase4_architecture_stub() -> Dict[str, Any]:
    """Return the ``BrandArchitectureOutput`` dummy payload.

    Preconditions: none.
    Postconditions: returns a fresh dict matching that schema's field set.
    """
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


def _phase4_brand_in_action_stub() -> Dict[str, Any]:
    """Return the ``BrandInActionOutput`` dummy payload.

    Preconditions: none.
    Postconditions: returns a fresh dict matching that schema's field set.
    """
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


_PHASE4_STRUCTURED_OUTPUT_MODEL_NAMES: frozenset[str] = frozenset(
    {
        "BrandExperiencePrinciplesOutput",
        "ChannelGuidelineOutput",
        "BrandArchitectureOutput",
        "BrandInActionOutput",
    }
)


def _branding_phase4_structured_output_stub(
    model_name: str, system_lowered: str = ""
) -> Optional[Dict[str, Any]]:
    """Deterministic Branding Phase 4 stub for a known ``structured_output`` class name.

    Preconditions:
        ``model_name`` is a string (typically ``type.__name__``);
        ``system_lowered`` is already lowercased (may be empty) and is used
        only to fill ``channel`` for ``ChannelGuidelineOutput``.
    Postconditions:
        Returns the matching Phase 4 stub dict, or ``None`` for unrecognized names.
    """
    if model_name == "BrandExperiencePrinciplesOutput":
        return _phase4_experience_principles_stub()
    if model_name == "ChannelGuidelineOutput":
        return _phase4_channel_guide_stub(_phase4_channel_value(system_lowered))
    if model_name == "BrandArchitectureOutput":
        return _phase4_architecture_stub()
    if model_name == "BrandInActionOutput":
        return _phase4_brand_in_action_stub()
    return None


def _branding_phase4_text_routed_stub(system_lowered: str) -> Optional[Dict[str, Any]]:
    """Text-anchor fallback for Phase 4 branding structured-output stubs.

    Preconditions:
        ``system_lowered`` is the agent system prompt already lowercased (may be empty).
    Postconditions:
        Returns a dict that validates against the matching Phase 4 agent
        ``structured_output`` schema, or ``None`` when no Phase 4 agent matches.
        Covers all four distinct Phase 4 schemas: experience principles,
        the six channel guides (shared ``ChannelGuidelineOutput``),
        architecture, and brand-in-action.
    """
    if "content_types" in system_lowered and "frequency_guidance" in system_lowered:
        return _phase4_channel_guide_stub(_phase4_channel_value(system_lowered))
    if "brand_experience_principles" in system_lowered and "sensory_elements" in system_lowered:
        return _phase4_experience_principles_stub()
    if "brand_architecture" in system_lowered and "terminology_glossary" in system_lowered:
        return _phase4_architecture_stub()
    if "correct_example" in system_lowered and "incorrect_example" in system_lowered:
        return _phase4_brand_in_action_stub()
    return None
```

Also update `_branding_structured_stub` to call `_branding_phase4_text_routed_stub` instead of `_branding_phase4_structured_stub`, and fix its docstring references accordingly. Leave the Phase 5 call as-is until Task 2 renames it.

- [ ] **Step 2: Smoke-verify Phase 4 model routing (no new test file)**

Run from worktree `backend/`:

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python - <<'PY'
from llm_service.clients.dummy import (
    _branding_phase4_structured_output_stub,
    _branding_phase4_text_routed_stub,
)

assert _branding_phase4_structured_output_stub("Nope") is None
exp = _branding_phase4_structured_output_stub("BrandExperiencePrinciplesOutput")
assert "brand_experience_principles" in exp
ch = _branding_phase4_structured_output_stub(
    "ChannelGuidelineOutput", "channel: 'website'\ncontent_types frequency_guidance"
)
assert ch["channel"] == "website"
assert _branding_phase4_structured_output_stub("ChannelGuidelineOutput")["channel"] == "channel"
assert (
    _branding_phase4_text_routed_stub(
        "content_types frequency_guidance channel: 'social'"
    )["channel"]
    == "social"
)
print("phase4 ok")
PY
```

Expected: `phase4 ok`

- [ ] **Step 3: Commit**

```bash
git add backend/agents/llm_service/clients/dummy.py
git commit -m "$(cat <<'EOF'
Extract Phase 4 branding stubs and add model-class routing.

EOF
)"
```

---

### Task 2: Phase 5 payload helpers + model-name stub + text-route rename

**Files:**
- Modify: `backend/agents/llm_service/clients/dummy.py` (replace remaining `_branding_phase5_structured_stub` block; update `_branding_structured_stub`)

**Interfaces:**
- Consumes: existing inline Phase 5 stub dicts (move verbatim)
- Produces:
  - `_phase5_ownership_stub() -> Dict[str, Any]`
  - `_phase5_approval_workflows_stub() -> Dict[str, Any]`
  - `_phase5_asset_wiki_stub() -> Dict[str, Any]`
  - `_phase5_training_stub() -> Dict[str, Any]`
  - `_phase5_kpi_stub() -> Dict[str, Any]`
  - `_phase5_evolution_stub() -> Dict[str, Any]`
  - `_phase5_brand_guidelines_stub() -> Dict[str, Any]`
  - `_PHASE5_STRUCTURED_OUTPUT_MODEL_NAMES: frozenset[str]`
  - `_branding_phase5_structured_output_stub(model_name: str) -> Optional[Dict[str, Any]]`
  - `_branding_phase5_text_routed_stub(system_lowered: str) -> Optional[Dict[str, Any]]`

- [ ] **Step 1: Replace `_branding_phase5_structured_stub` with payload helpers, model stub, and text router**

Delete `def _branding_phase5_structured_stub(...)` through its final `return None` and insert (payloads must match today's literals exactly):

```python
def _phase5_ownership_stub() -> Dict[str, Any]:
    """Return the ``OwnershipOutput`` dummy payload.

    Preconditions: none.
    Postconditions: returns a fresh dict matching that schema's field set.
    """
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


def _phase5_approval_workflows_stub() -> Dict[str, Any]:
    """Return the ``ApprovalWorkflowsOutput`` dummy payload.

    Preconditions: none.
    Postconditions: returns a fresh dict matching that schema's field set.
    """
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


def _phase5_asset_wiki_stub() -> Dict[str, Any]:
    """Return the ``AssetWikiOutput`` dummy payload.

    Preconditions: none.
    Postconditions: returns a fresh dict matching that schema's field set.
    """
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


def _phase5_training_stub() -> Dict[str, Any]:
    """Return the ``TrainingOnboardingOutput`` dummy payload.

    Preconditions: none.
    Postconditions: returns a fresh dict matching that schema's field set.
    """
    return {
        "training_onboarding_plan": [
            "New-hire brand orientation session in week one (dummy).",
            "Quarterly brand refresher workshop (dummy).",
            "Self-serve brand guideline course in the LMS (dummy).",
            "Office-hours with the Brand team for open questions (dummy).",
        ],
    }


def _phase5_kpi_stub() -> Dict[str, Any]:
    """Return the ``BrandHealthKPIsOutput`` dummy payload.

    Preconditions: none.
    Postconditions: returns a fresh dict matching that schema's field set.
    """
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


def _phase5_evolution_stub() -> Dict[str, Any]:
    """Return the ``EvolutionFrameworkOutput`` dummy payload.

    Preconditions: none.
    Postconditions: returns a fresh dict matching that schema's field set.
    """
    return {
        "evolution_framework": (
            "The brand evolves incrementally through versioned updates, with major shifts "
            "reserved for strategic inflection points (dummy)."
        ),
        "version_control_cadence": (
            "Formal review every two quarters, with minor patches as needed (dummy)."
        ),
    }


def _phase5_brand_guidelines_stub() -> Dict[str, Any]:
    """Return the ``BrandGuidelinesOutput`` dummy payload.

    Preconditions: none.
    Postconditions: returns a fresh dict matching that schema's field set.
    """
    return {
        "brand_guidelines": [
            "Always use the approved wordmark; never recreate it (dummy).",
            "Lead every message with the customer outcome, not the feature (dummy).",
            "All external assets require Brand Director sign-off before release (dummy).",
            "Store approved assets only in the central DAM (dummy).",
            "Review the brand system every two quarters (dummy).",
        ],
    }


_PHASE5_STRUCTURED_OUTPUT_MODEL_NAMES: frozenset[str] = frozenset(
    {
        "OwnershipOutput",
        "ApprovalWorkflowsOutput",
        "AssetWikiOutput",
        "TrainingOnboardingOutput",
        "BrandHealthKPIsOutput",
        "EvolutionFrameworkOutput",
        "BrandGuidelinesOutput",
    }
)


def _branding_phase5_structured_output_stub(model_name: str) -> Optional[Dict[str, Any]]:
    """Deterministic Branding Phase 5 stub for a known ``structured_output`` class name.

    Preconditions:
        ``model_name`` is a string (typically ``type.__name__``).
    Postconditions:
        Returns the matching Phase 5 stub dict, or ``None`` for unrecognized names.
    """
    if model_name == "OwnershipOutput":
        return _phase5_ownership_stub()
    if model_name == "ApprovalWorkflowsOutput":
        return _phase5_approval_workflows_stub()
    if model_name == "AssetWikiOutput":
        return _phase5_asset_wiki_stub()
    if model_name == "TrainingOnboardingOutput":
        return _phase5_training_stub()
    if model_name == "BrandHealthKPIsOutput":
        return _phase5_kpi_stub()
    if model_name == "EvolutionFrameworkOutput":
        return _phase5_evolution_stub()
    if model_name == "BrandGuidelinesOutput":
        return _phase5_brand_guidelines_stub()
    return None


def _branding_phase5_text_routed_stub(system_lowered: str) -> Optional[Dict[str, Any]]:
    """Text-anchor fallback for Phase 5 branding structured-output stubs.

    Preconditions:
        ``system_lowered`` is the agent system prompt already lowercased (may be empty).
    Postconditions:
        Returns a dict that validates against the matching Phase 5 agent
        ``structured_output`` schema, or ``None`` when no Phase 5 agent matches.
        Covers all seven Phase 5 factories.
    """
    if "ownership_model" in system_lowered and "decision_authority" in system_lowered:
        return _phase5_ownership_stub()
    if "approval_workflows" in system_lowered and "agency_briefing_protocols" in system_lowered:
        return _phase5_approval_workflows_stub()
    if "asset_management_guidance" in system_lowered and "wiki_backlog" in system_lowered:
        return _phase5_asset_wiki_stub()
    if "training_onboarding_plan" in system_lowered and "brand literacy" in system_lowered:
        return _phase5_training_stub()
    if "brand_health_kpis" in system_lowered and "tracking_methodology" in system_lowered:
        return _phase5_kpi_stub()
    if "evolution_framework" in system_lowered and "version_control_cadence" in system_lowered:
        return _phase5_evolution_stub()
    if "brand_guidelines" in system_lowered and "governance rules" in system_lowered:
        return _phase5_brand_guidelines_stub()
    return None
```

Update `_branding_structured_stub` so it calls `_branding_phase4_text_routed_stub` and `_branding_phase5_text_routed_stub`, and update its docstring to name those helpers.

- [ ] **Step 2: Smoke-verify Phase 5 model routing**

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python - <<'PY'
from llm_service.clients.dummy import (
    _branding_phase5_structured_output_stub,
    _branding_phase5_text_routed_stub,
    _branding_structured_stub,
)

assert _branding_phase5_structured_output_stub("Nope") is None
assert "ownership_model" in _branding_phase5_structured_output_stub("OwnershipOutput")
assert "brand_guidelines" in _branding_phase5_structured_output_stub("BrandGuidelinesOutput")
assert (
    _branding_phase5_text_routed_stub("ownership_model decision_authority")[
        "decision_authority"
    ]["logo_changes"]
    == "Brand Director"
)
# aggregator still reaches Phase 5 via text
assert _branding_structured_stub("ownership_model decision_authority") is not None
print("phase5 ok")
PY
```

Expected: `phase5 ok`

- [ ] **Step 3: Commit**

```bash
git add backend/agents/llm_service/clients/dummy.py
git commit -m "$(cat <<'EOF'
Extract Phase 5 branding stubs and add model-class routing.

EOF
)"
```

---

### Task 3: Wire resolver into `complete_json` / `chat` / `stream` and tool detection

**Files:**
- Modify: `backend/agents/llm_service/clients/dummy.py`
  - Add `_branding_structured_output_stub_by_model_name` near the Phase 2 stub (after Phase 5 frozenset exists — place it after `_branding_phase5_structured_output_stub`)
  - Update `_looks_like_structured_output_tool`
  - Update `complete_json` deterministic branch
  - Update `stream` deterministic resolution
  - Update `chat` deterministic resolution

**Interfaces:**
- Consumes: `_branding_phase2_structured_output_stub`, `_branding_phase4_structured_output_stub`, `_branding_phase5_structured_output_stub`, Phase 4/5 name frozensets
- Produces: `_branding_structured_output_stub_by_model_name(model_name: str, system_lowered: str = "") -> Optional[Dict[str, Any]]`

- [ ] **Step 1: Add the Phase 2→4→5 resolver**

Insert after `_branding_phase5_structured_output_stub`:

```python
def _branding_structured_output_stub_by_model_name(
    model_name: str, system_lowered: str = ""
) -> Optional[Dict[str, Any]]:
    """Resolve a branding stub by structured-output model class name.

    Tries Phase 2, then Phase 4, then Phase 5. Shared by ``complete_json``,
    ``chat``, and ``stream`` so all three keep one precedence order.

    Preconditions:
        ``model_name`` is a string; ``system_lowered`` is already lowercased
        (may be empty) and is forwarded to Phase 4 for channel extraction only.
    Postconditions:
        Returns the first matching stub dict, or ``None`` so callers fall
        through to text-anchor / generic paths.
    """
    stub = _branding_phase2_structured_output_stub(model_name)
    if stub is not None:
        return stub
    stub = _branding_phase4_structured_output_stub(model_name, system_lowered)
    if stub is not None:
        return stub
    return _branding_phase5_structured_output_stub(model_name)
```

- [ ] **Step 2: Extend `_looks_like_structured_output_tool`**

Change the return to include Phase 4/5 membership, and update the docstring so it no longer says "six known Phase 2 classes" only:

```python
    return (
        name == "structured_output"
        or "structuredoutputtool" in description_lowered
        or "structured_output" in description_lowered
        or name in _PHASE2_STRUCTURED_OUTPUT_MODEL_NAMES
        or name in _PHASE4_STRUCTURED_OUTPUT_MODEL_NAMES
        or name in _PHASE5_STRUCTURED_OUTPUT_MODEL_NAMES
    )
```

- [ ] **Step 3: Wire `complete_json`**

Replace the Phase-2-only deterministic block with:

```python
        if structured_output_model is not None:
            assert isinstance(structured_output_model, type), (
                f"structured_output_model must be a Pydantic model class, "
                f"got {structured_output_model!r}"
            )
            deterministic = _branding_structured_output_stub_by_model_name(
                structured_output_model.__name__,
                (system_prompt or "").lower(),
            )
            if deterministic is not None:
                return deterministic
```

Update the surrounding comment so it describes Phase 2/4/5 model-name routing (not Phase 2 only). Confirm `system_prompt` is in scope in `complete_json` at this point (it is — the method accepts `system_prompt: Optional[str] = None`).

- [ ] **Step 4: Wire `stream`**

Replace:

```python
        deterministic = (
            _branding_phase2_structured_output_stub(structured_tool_name)
            if structured_tool_name
            else None
        )
```

with:

```python
        deterministic = (
            _branding_structured_output_stub_by_model_name(
                structured_tool_name,
                (system_prompt or "").lower(),
            )
            if structured_tool_name
            else None
        )
```

- [ ] **Step 5: Wire `chat`**

Replace:

```python
                data = _branding_phase2_structured_output_stub(structured_tool.get("name") or "")
```

with:

```python
                data = _branding_structured_output_stub_by_model_name(
                    structured_tool.get("name") or "",
                    (system_prompt or "").lower(),
                )
```

Update `chat`'s docstring bullet that currently mentions only `_branding_phase2_structured_output_stub` so it names the Phase 2→4→5 resolver instead.

- [ ] **Step 6: Smoke-verify end-to-end model routing on `complete_json` / `chat`**

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python - <<'PY'
from llm_service.clients.dummy import DummyLLMClient, _branding_phase4_structured_output_stub

class ChannelGuidelineOutput:  # name-only stand-in; complete_json routes on __name__
    pass

class OwnershipOutput:
    pass

c = DummyLLMClient()
# Misleading prompt must not override model-class routing
out = c.complete_json(
    "please respond",
    system_prompt="ownership_model decision_authority unrelated",
    structured_output_model=ChannelGuidelineOutput,
)
assert out == _branding_phase4_structured_output_stub(
    "ChannelGuidelineOutput", "ownership_model decision_authority unrelated"
)
assert out["channel"] == "channel"

own = c.complete_json(
    "please respond",
    system_prompt="content_types frequency_guidance channel: 'website'",
    structured_output_model=OwnershipOutput,
)
assert "ownership_model" in own

# chat path
tools = [
    {
        "type": "function",
        "function": {
            "name": "ChannelGuidelineOutput",
            "description": "IMPORTANT: This StructuredOutputTool should only be invoked...",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]
result = c.chat(
    [
        {"role": "system", "content": "channel: 'email'\ncontent_types frequency_guidance"},
        {"role": "user", "content": "go"},
    ],
    tools=tools,
)
args = result["__tool_calls__"][0]["function"]["arguments"]
assert args["channel"] == "email"
print("wiring ok")
PY
```

Expected: `wiring ok`

- [ ] **Step 7: Commit**

```bash
git add backend/agents/llm_service/clients/dummy.py
git commit -m "$(cat <<'EOF'
Wire Phase 4/5 model-class stub routing into DummyLLMClient call sites.

EOF
)"
```

---

### Task 4: Lint + existing suite verification

**Files:**
- Verify only: `backend/agents/llm_service/clients/dummy.py`

**Interfaces:** none new

- [ ] **Step 1: Lint the touched file**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/5558-branding-phase4-5-stub-routing/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/ruff check agents/llm_service/clients/dummy.py
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/ruff format --check agents/llm_service/clients/dummy.py
```

Expected: no issues. If format drifts, run `ruff format agents/llm_service/clients/dummy.py` and amend only if the previous commit was yours and unpushed; otherwise make a new commit.

- [ ] **Step 2: Run existing related tests**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/5558-branding-phase4-5-stub-routing/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/llm_service/tests/test_dummy_client.py \
  agents/branding_team/tests/test_dummy_structured_output_contract.py \
  agents/branding_team/tests/test_dummy_stub_alignment.py \
  -q
```

Expected: all pass. Text-routed Phase 4/5 cases must still pass via the renamed helpers; Phase 2 model-routed cases must still pass via the shared resolver.

- [ ] **Step 3: Final status check**

```bash
git status -sb
git log --oneline origin/main..HEAD
```

Confirm only the intended commits are on the branch (design doc + Tasks 1–3; format fix commit if any).

---

## Plan self-review

1. **Spec coverage:** Goal, Phase 4/5 dispatch tables, channel extraction, text-route rename, frozensets, call-site wiring (`complete_json`/`chat`/`stream`), tool membership, no new tests, DbC — all mapped to Tasks 1–4.
2. **Placeholders:** none.
3. **Type consistency:** `_branding_phase4_structured_output_stub(model_name, system_lowered="")`, `_branding_phase5_structured_output_stub(model_name)`, `_branding_structured_output_stub_by_model_name(model_name, system_lowered="")` used consistently across tasks.
