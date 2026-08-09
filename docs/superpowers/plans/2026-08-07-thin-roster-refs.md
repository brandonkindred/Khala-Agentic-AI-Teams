# Thin Roster Refs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist `AgenticTeamAgent` as thin refs (`agent_name`, `source`, `manifest_id`) with join-at-read Manifest persona resolution so Stage-3 mixed rosters stop treating fat roster rows as a second agent SoT.

**Architecture:** Add `roster_resolve.py` for legacy migrate + `resolve_persona` → `RosterPersonaView`. Thin the Pydantic model; change Manifest generation to take `(team_id, agent_name, summary=...)` instead of reading `agent.role`. API list/GET enrich from Manifest; fat PUT returns 400. Pipeline/test-chat/validation/recommend call the resolver then reuse existing `build_agent`.

**Tech Stack:** Python 3.10+, Pydantic v2, FastAPI, Postgres JSONB roster store, `agent_registry`, Angular 19 / Vitest for minimal frontend touch

## Global Constraints

- Work only in worktree `.worktrees/5696-thin-roster-refs` on branch `feature/5696-thin-roster-refs`
- Follow design: `docs/superpowers/specs/2026-08-07-thin-roster-refs-design.md`
- Design-by-Contract docstrings (`Preconditions:` / `Postconditions:` / `Invariants:` where relevant) on every new public function/method/module
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; Python 3.10 target
- Coverage ≥ 90% on new/changed backend and frontend code
- Do not implement Manifest `states[]` prompt binding or typed registry DAG invoke
- Do not proxy roster PUT into Manifest field edits
- `investment_team.agent_catalog.AgentDefinition` is out of scope

## File map

| File | Role |
|---|---|
| `agentic_team_provisioning/roster_resolve.py` | **Create** — migrate legacy rows, `RosterPersonaView`, `resolve_persona`, enrich helper |
| `agentic_team_provisioning/models.py` | Thin `AgenticTeamAgent`; `RosterPersonaView` / `EnrichedRosterAgent`; retire fat `UpdateAgentRequest` fields (or keep model but API rejects) |
| `agentic_team_provisioning/manifest_generation.py` | `build_agent_manifest(team_id, agent_name, *, summary=None)`; register path works with thin refs |
| `agentic_team_provisioning/assistant/store.py` | Parse legacy JSON → migrate/persist thin on load/save |
| `agentic_team_provisioning/api/main.py` | Thin from-registry; LLM save → Manifest then thin; enrich GET; PUT → 400; recommend/validation resolve |
| `agentic_team_provisioning/runtime/pipeline_runner.py` | Resolve persona before `build_agent` |
| `agentic_team_provisioning/runtime/agent_builder.py` | Optional thin wrapper `build_agent_from_persona(view)` — keep existing `build_agent` signature |
| `agentic_team_provisioning/roster_validation.py` | Depth/coverage against resolved persona maps |
| Frontend `agentic-team.model.ts` + process-designer | Thin types; read-only chips; stop fat PUT |
| Tests under `agentic_team_provisioning/tests/` | Update fat assumptions; add migrate/resolve/PUT-400 cases |

---

### Task 1: `RosterPersonaView` + `resolve_persona`

**Files:**
- Create: `backend/agents/agent_team_studio/agentic_team_provisioning/roster_resolve.py`
- Create: `backend/agents/agent_team_studio/agentic_team_provisioning/tests/test_roster_resolve.py`

**Interfaces:**
- Consumes: `agent_registry.get_registry`, `AgentManifest`
- Produces:
  - `class RosterPersonaView(BaseModel)` with `role: str`, `skills: list[str]`, `capabilities: list[str]`, `tools: list[str]`, `expertise: list[str]`
  - `persona_from_manifest(manifest: AgentManifest) -> RosterPersonaView`
  - `resolve_persona(manifest_id: str) -> RosterPersonaView` (raises `LookupError` if missing)

- [ ] **Step 1: Write the failing tests**

```python
"""Join-at-read Manifest → RosterPersonaView."""

from __future__ import annotations

import pytest
from agent_registry.models import AgentManifest, CognitionSpec, SourceInfo

from agent_team_studio.agentic_team_provisioning.roster_resolve import (
    persona_from_manifest,
    resolve_persona,
)


def _manifest(**kwargs) -> AgentManifest:
    base = dict(
        id="demo.planner",
        team="demo",
        name="Planner",
        summary="Plans work",
        tags=["planning"],
        cognition=CognitionSpec(tools=["web_search"]),
        source=SourceInfo(entrypoint="demo.planner:run"),
    )
    base.update(kwargs)
    return AgentManifest.model_validate(base)


def test_persona_from_manifest_maps_summary_tags_tools_team() -> None:
    view = persona_from_manifest(_manifest())
    assert view.role == "Plans work"
    assert view.skills == ["planning"]
    assert view.tools == ["web_search"]
    assert view.expertise == ["demo"]
    assert view.capabilities == []


def test_persona_from_manifest_empty_cognition_tools() -> None:
    view = persona_from_manifest(_manifest(cognition=None, tags=[]))
    assert view.tools == []
    assert view.skills == []
    assert view.expertise == ["demo"]


def test_resolve_persona_looks_up_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _manifest()

    class _Reg:
        def get(self, agent_id: str):
            return m if agent_id == m.id else None

    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.roster_resolve.get_registry",
        lambda: _Reg(),
    )
    assert resolve_persona("demo.planner").role == "Plans work"


def test_resolve_persona_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Reg:
        def get(self, agent_id: str):
            return None

    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.roster_resolve.get_registry",
        lambda: _Reg(),
    )
    with pytest.raises(LookupError):
        resolve_persona("missing.id")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest agents/agent_team_studio/agentic_team_provisioning/tests/test_roster_resolve.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Write minimal implementation**

```python
"""Join-at-read roster persona resolution from AgentManifest (Identity SoT)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent_registry import get_registry
from agent_registry.models import AgentManifest


class RosterPersonaView(BaseModel):
    """Non-persisted free-text persona projected from a Manifest.

    Invariants:
        * Never written to ``agentic_team_agents``; roster stores thin refs only.
    """

    role: str = ""
    skills: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    expertise: list[str] = Field(default_factory=list)


def persona_from_manifest(manifest: AgentManifest) -> RosterPersonaView:
    """Map Manifest fields to the free-text persona view used by ``build_agent``.

    Preconditions: ``manifest`` is a validated ``AgentManifest``.
    Postconditions: ``role`` is ``summary`` (or ``name`` if summary blank);
        ``skills`` ← ``tags``; ``tools`` ← ``cognition.tools`` or ``[]``;
        ``expertise`` ← ``[team]`` when team non-empty; ``capabilities`` always ``[]``.
    """
    tools: list[str] = []
    if manifest.cognition and manifest.cognition.tools:
        tools = list(manifest.cognition.tools)
    return RosterPersonaView(
        role=(manifest.summary or manifest.name or "").strip(),
        skills=list(manifest.tags or []),
        capabilities=[],
        tools=tools,
        expertise=[manifest.team] if manifest.team else [],
    )


def resolve_persona(manifest_id: str) -> RosterPersonaView:
    """Load Manifest by id and project persona.

    Preconditions: ``manifest_id`` is a non-empty string.
    Postconditions: returns ``persona_from_manifest`` for the registry entry.
    Raises: ``LookupError`` if the Manifest is not in the registry.
    """
    if not manifest_id or not str(manifest_id).strip():
        raise LookupError("manifest_id must be non-empty")
    manifest = get_registry().get(manifest_id)
    if manifest is None:
        raise LookupError(f"AgentManifest not found: {manifest_id}")
    return persona_from_manifest(manifest)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest agents/agent_team_studio/agentic_team_provisioning/tests/test_roster_resolve.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -f backend/agents/agent_team_studio/agentic_team_provisioning/roster_resolve.py \
  backend/agents/agent_team_studio/agentic_team_provisioning/tests/test_roster_resolve.py
git commit -m "$(cat <<'EOF'
Add Manifest join-at-read roster persona resolver.

EOF
)"
```

---

### Task 2: Legacy migrate helper (fat JSON → thin + stamp `manifest_id`)

**Files:**
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/roster_resolve.py`
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/tests/test_roster_resolve.py`

**Interfaces:**
- Consumes: `manifest_agent_id`, `build_agent_manifest`, `get_registry().register` (or look up existing id)
- Produces:
  - `migrate_roster_row(team_id: str, raw: dict) -> tuple[AgenticTeamAgent, bool]`
    — returns thin agent and `changed` flag (True if persistence should rewrite)
  - Uses **current fat** `AgenticTeamAgent` temporarily in this task only if model not yet thinned; prefer operating on `raw: dict` so Task 3 can thin the model next

**Important:** Implement migrate against `raw: dict` + construct thin shape via `model_construct` or a small `ThinRosterRef` dataclass until Task 3 lands. After Task 3, return `AgenticTeamAgent`.

- [ ] **Step 1: Write the failing tests** (append to `test_roster_resolve.py`)

```python
from agent_team_studio.agentic_team_provisioning.manifest_generation import manifest_agent_id
from agent_team_studio.agentic_team_provisioning.roster_resolve import migrate_roster_row


def test_migrate_generated_stamps_manifest_id_and_strips_fat(monkeypatch):
    team_id = "team-1"
    raw = {
        "agent_name": "Writer",
        "role": "Writes docs",
        "skills": ["seo"],
        "capabilities": [],
        "tools": [],
        "expertise": [],
        "source": "generated",
        "manifest_id": None,
    }
    expected_id = manifest_agent_id(team_id, "Writer")

    class _Reg:
        def __init__(self):
            self._m = {}

        def get(self, agent_id: str):
            return self._m.get(agent_id)

        def register(self, manifest, source_path=None, *, require_persist: bool = False):
            self._m[manifest.id] = manifest

    reg = _Reg()
    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.roster_resolve.get_registry",
        lambda: reg,
    )
    # Also patch build path's get_registry if register goes through loader
    monkeypatch.setattr("agent_registry.get_registry", lambda: reg)

    agent, changed = migrate_roster_row(team_id, raw)
    assert changed is True
    assert agent.agent_name == "Writer"
    assert agent.source == "generated"
    assert agent.manifest_id == expected_id
    assert not hasattr(agent, "role") or "role" not in agent.model_fields_set  # after thin model
    # After Task 3 thin model: only three fields exist
    assert set(agent.model_dump()) == {"agent_name", "source", "manifest_id"}


def test_migrate_registry_without_manifest_id_raises():
    with pytest.raises(ValueError, match="manifest_id"):
        migrate_roster_row(
            "team-1",
            {"agent_name": "X", "source": "registry", "manifest_id": None, "role": "r"},
        )


def test_migrate_already_thin_unchanged():
    raw = {
        "agent_name": "Writer",
        "source": "generated",
        "manifest_id": "agentic_team_provisioning.abc.writer-1",
    }
    agent, changed = migrate_roster_row("team-1", raw)
    assert changed is False
    assert agent.manifest_id == raw["manifest_id"]
```

- [ ] **Step 2: Run tests — expect FAIL** (`migrate_roster_row` missing)

- [ ] **Step 3: Implement `migrate_roster_row`**

Logic (match design):

1. Read `agent_name`, `source` (default `"generated"`), `manifest_id` from `raw`.
2. If `manifest_id` present and non-empty → return thin agent, `changed=False` if raw had only thin keys; if fat keys present still return thin and `changed=True` to strip.
3. If `source == "registry"` and no `manifest_id` → `raise ValueError`.
4. If `source == "generated"` and no `manifest_id`:
   - `mid = manifest_agent_id(team_id, agent_name)`
   - If `get_registry().get(mid)` missing → `build_agent_manifest(team_id, agent_name, summary=raw.get("role") or None)` and `register(...)` (use whatever register API exists on the loader).
   - Set `manifest_id = mid`
5. Return thin `AgenticTeamAgent(agent_name=..., source=..., manifest_id=...)`, `changed=True`

**Note:** If `build_agent_manifest` still requires fat `AgenticTeamAgent` until Task 4, call it with a temporary object or implement Task 4's signature first inside this commit. Prefer doing the `build_agent_manifest(team_id, agent_name, *, summary=)` signature change in **this** task if migrate needs it — keep Task 4 for updating all call sites.

- [ ] **Step 4: Tests PASS**

- [ ] **Step 5: Commit**

```bash
git commit -m "$(cat <<'EOF'
Add eager migrate for legacy fat roster rows to thin refs.

EOF
)"
```

---

### Task 3: Thin `AgenticTeamAgent` model + store load/save migration

**Files:**
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/models.py` (`AgenticTeamAgent`, docstring; keep `UpdateAgentRequest` for now but unused for writes)
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/assistant/store.py` (`_load_team_agents`, `_get_team_agent`, upsert paths)
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/tests/test_team_agents.py` and any constructor call sites broken by removing `role`

**Interfaces:**
- Produces: `AgenticTeamAgent` with only `agent_name: str`, `source: Literal[...]`, `manifest_id: str` (required, min_length=1)
- Store: on load, if validate fails or fat keys present, call `migrate_roster_row` and optionally rewrite row when `changed`

- [ ] **Step 1: Write/adjust a focused store migrate test**

```python
def test_list_team_agents_migrates_fat_row(store, monkeypatch):
    # insert raw fat JSON via SQL or save pre-thin fixture
    # list_team_agents returns thin agent with manifest_id
    # second list is no-op (changed=False)
    ...
```

Use existing Postgres test fixtures in this package (follow `test_team_agents.py` patterns).

- [ ] **Step 2: Run — FAIL on `role` required / fat constructors**

- [ ] **Step 3: Thin the model**

```python
class AgenticTeamAgent(BaseModel):
    """Thin roster reference to a registry AgentManifest.

    Invariants:
        * ``manifest_id`` is always set for persisted rows (enforced after migrate).
        * ``agent_name`` is the team-local slot key; may differ from ``manifest.name``.
        * Persona fields are not stored here — resolve via ``roster_resolve``.
    """

    agent_name: str = Field(..., description="Stable, unique slot name within the team")
    source: Literal[SOURCE_GENERATED, SOURCE_REGISTRY] = Field(default=SOURCE_GENERATED)
    manifest_id: str = Field(..., min_length=1, description="AgentManifest id (SoT join key)")
```

Update `_load_team_agents`:

```python
def _load_team_agents(self, cur, team_id: str) -> list[AgenticTeamAgent]:
    cur.execute(
        "SELECT data_json FROM agentic_team_agents WHERE team_id = %s ORDER BY agent_name",
        (team_id,),
    )
    agents: list[AgenticTeamAgent] = []
    for r in cur.fetchall():
        raw = r["data_json"]
        if isinstance(raw, str):
            import json
            raw = json.loads(raw)
        agent, changed = migrate_roster_row(team_id, dict(raw))
        if changed:
            # persist thin — reuse existing upsert helper used by save_team_agent
            self._upsert_team_agent_row(cur, team_id, agent)  # extract if needed
        agents.append(agent)
    return agents
```

Fix every test constructor `AgenticTeamAgent(agent_name=..., role=...)` → supply `manifest_id=...` (use `manifest_agent_id` or a fixture id). Do the minimal set so unit tests collect; remaining suites fixed in later tasks.

- [ ] **Step 4: Run** `pytest .../tests/test_team_agents.py .../tests/test_roster_resolve.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "$(cat <<'EOF'
Thin AgenticTeamAgent to slot key, source, and manifest_id.

EOF
)"
```

---

### Task 4: Manifest generation without fat `agent.role`

**Files:**
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/manifest_generation.py`
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/tests/test_manifest_generation.py`
- Modify: call sites in `api/main.py` (`_unregister_generated_manifest`, `_reregister_generated_manifest`) — unregister uses `manifest_agent_id(team_id, agent.agent_name)` or `agent.manifest_id` directly

**Interfaces:**
- Produces: `build_agent_manifest(team_id: str, agent_name: str, *, summary: str | None = None) -> AgentManifest`
- `register_team_manifests(team_id, agents, *, summaries: dict[str, str] | None = None, conn=None)`:
  - For each generated thin agent, `summary = (summaries or {}).get(name)`; else existing registry summary; else default

- [ ] **Step 1: Update failing tests in `test_manifest_generation.py`**

Change calls from `build_agent_manifest("t", AgenticTeamAgent(...))` to `build_agent_manifest("t", "QA Agent", summary="r")`.

- [ ] **Step 2: Run — FAIL on signature**

- [ ] **Step 3: Implement new signature**

```python
def build_agent_manifest(
    team_id: str,
    agent_name: str,
    *,
    summary: str | None = None,
) -> AgentManifest:
    if not team_id:
        raise ValueError("build_agent_manifest: team_id must be non-empty")
    if not agent_name:
        raise ValueError("build_agent_manifest: agent_name must be non-empty")
    manifest_id = manifest_agent_id(team_id, agent_name)
    resolved_summary = (summary or "").strip() or f"Generated agent {agent_name}"
    manifest = AgentManifest(
        id=manifest_id,
        team=_TEAM_KEY,
        name=agent_name,
        summary=resolved_summary,
        tags=["generated", _TEAM_KEY],
        inputs=IOSchema(schema_ref=_INPUT_SCHEMA_REF, description="..."),
        outputs=IOSchema(schema_ref=_OUTPUT_SCHEMA_REF, description="..."),
        cognition=default_cognition_block(),
        source=SourceInfo(entrypoint=_ENTRYPOINT, anatomy_ref=_ANATOMY_REF),
    )
    return AgentManifest.model_validate(manifest.model_dump(mode="json"))
```

Update `register_team_manifests` loop accordingly. Unregister hooks: `get_registry().unregister(agent.manifest_id)`.

- [ ] **Step 4: `pytest .../tests/test_manifest_generation.py -v` → PASS**

- [ ] **Step 5: Commit**

```bash
git commit -m "$(cat <<'EOF'
Build generated manifests from agent name and summary, not fat roster fields.

EOF
)"
```

---

### Task 5: API — from-registry thin, enrich list, fat PUT → 400, LLM save

**Files:**
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/api/main.py`
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/models.py` — add `EnrichedRosterAgent` (thin fields + persona view fields flattened for UI)
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/tests/test_registry_roster.py`

**Interfaces:**
- `_roster_agent_from_manifest(manifest) -> AgenticTeamAgent` returns thin only:
  `AgenticTeamAgent(agent_name=manifest.name, source=SOURCE_REGISTRY, manifest_id=manifest.id)`
- `enrich_roster_agent(agent) -> EnrichedRosterAgent` = thin dump + `resolve_persona(agent.manifest_id)` fields
- `update_roster_agent` → `raise HTTPException(400, detail="Roster persona edits are not supported; AgentManifest is the source of truth")` without calling store merge
- `_save_agents_from_llm`: for each LLM dict, `build_agent_manifest(team_id, name, summary=role)`, collect manifests, `register_team_manifests` / replace with summaries map, then `merge_generated_agents` with thin refs (`manifest_id` set)

- [ ] **Step 1: Write/adjust API tests**

```python
def test_from_registry_stores_thin_ref(client, ...):
    ...
    row = ...
    assert set(row.keys()) >= {"agent_name", "source", "manifest_id"}
    assert "role" in row  # enriched response may still expose role
    # Persist check: reload from store / DB without enrich → no role key


def test_update_roster_agent_rejects_fat_put(client, ...):
    r = client.put(f"/teams/{tid}/agents/Writer", json={"role": "new"})
    assert r.status_code == 400


def test_llm_save_stamps_manifest_id_on_generated(...):
    ...
    assert roster[0]["manifest_id"]  # not None
```

Remove/replace assertions that `manifest_id is None` for generated rows.

- [ ] **Step 2: Run subset — FAIL**

- [ ] **Step 3: Implement API changes**

`EnrichedRosterAgent` example:

```python
class EnrichedRosterAgent(BaseModel):
    agent_name: str
    source: Literal[SOURCE_GENERATED, SOURCE_REGISTRY]
    manifest_id: str
    role: str = ""
    skills: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    expertise: list[str] = Field(default_factory=list)
```

List endpoint `response_model=list[EnrichedRosterAgent]`.

- [ ] **Step 4: `pytest .../tests/test_registry_roster.py -v` → PASS**

- [ ] **Step 5: Commit**

```bash
git commit -m "$(cat <<'EOF'
Expose thin roster refs with Manifest enrichment; reject fat persona PUT.

EOF
)"
```

---

### Task 6: Runtime + validation + recommend use resolver

**Files:**
- Modify: `runtime/pipeline_runner.py` (`_run_agent`)
- Modify: test-chat helpers in `api/main.py` that call `build_agent` / `generate_starter_prompts`
- Modify: `roster_validation.py` — accept optional `personas: dict[str, RosterPersonaView]` or resolve inside `validate_roster` when agents are thin
- Modify: recommend-agents handler in `api/main.py`
- Tests: `test_pipeline_runner.py`, `test_roster_validation.py`, `test_runtime_cognition.py` as needed

**Interfaces:**
- `PipelineRunner._run_agent`:

```python
@staticmethod
def _run_agent(agent_def: AgenticTeamAgent, prompt: str) -> str:
    persona = resolve_persona(agent_def.manifest_id)
    agent_instance = build_agent(
        agent_def.agent_name,
        persona.role,
        persona.skills,
        persona.capabilities,
        persona.tools,
        persona.expertise,
    )
    return call_agent(agent_instance, prompt)
```

- Validation depth check: use persona lists from `resolve_persona` (or precomputed map) instead of `agent.skills` etc.

- [ ] **Step 1: Update one pipeline test to register a Manifest and thin roster row; assert `build_agent` receives mapped summary**

- [ ] **Step 2: Run — FAIL**

- [ ] **Step 3: Wire resolver at all four call sites**

- [ ] **Step 4: Run**  
  `pytest agents/agent_team_studio/agentic_team_provisioning/tests/test_pipeline_runner.py agents/agent_team_studio/agentic_team_provisioning/tests/test_roster_validation.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "$(cat <<'EOF'
Resolve roster personas from Manifest in pipeline, validation, and recommend.

EOF
)"
```

---

### Task 7: Frontend minimal + remaining test sweep

**Files:**
- Modify: `user-interface/src/app/models/agentic-team.model.ts`
- Modify: `user-interface/src/app/services/agentic-team-api.service.ts` (if update typing changes)
- Modify: `user-interface/src/app/components/process-designer-chat/process-designer-chat.component.ts` (+ template) — read-only persona chips; remove/disable fat save PUT
- Modify: related `*.spec.ts`
- Sweep remaining backend tests: `test_temporal_dispatch.py`, `test_agent_manifests_endpoint.py`, `test_send_test_chat_message_atomicity.py`, `test_conversation_registry_failure.py`, etc.

**Interfaces:**
- TS:

```typescript
export interface AgenticTeamAgent {
  agent_name: string;
  source: AgenticTeamAgentSource;
  manifest_id: string;
  // Enriched (optional on wire — present on GET/list)
  role?: string;
  skills?: string[];
  capabilities?: string[];
  tools?: string[];
  expertise?: string[];
}
```

- [ ] **Step 1: Update frontend unit tests that construct fat agents / expect PUT**

- [ ] **Step 2: Run** `cd user-interface && npx vitest run src/app/models/agentic-team.model.ts src/app/components/process-designer-chat/process-designer-chat.component.spec.ts` (adjust paths to what exists)

- [ ] **Step 3: Implement TS + disable fat PUT in UI**

- [ ] **Step 4: Full backend package tests**

Run: `cd backend && python -m pytest agents/agent_team_studio/agentic_team_provisioning/tests -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "$(cat <<'EOF'
Align frontend roster types with thin refs and finish test migration.

EOF
)"
```

---

### Task 8: Docstring / architecture note cleanup (agentic paths only)

**Files:**
- Modify docstrings on `AgenticTeamAgent`, `AddAgentFromRegistryRequest`, `_roster_agent_from_manifest`, ADR-008 cross-comments that say roster stores projected persona as definition
- Optionally one paragraph in `docs/design/agent-studio-ux-spec.md` §5.3 if it still says roster PUT edits persona without mutating Manifest — point to Manifest SoT + read enrichment (no issue numbers)

- [ ] **Step 1: Grep for stale guidance**

```bash
rg -n "full agent definition|second source of truth|per-team override|AgenticTeamAgent" \
  backend/agents/agent_team_studio/agentic_team_provisioning \
  docs/design/agent-studio-ux-spec.md
```

- [ ] **Step 2: Update wording to thin ref + join-at-read**

- [ ] **Step 3: Commit**

```bash
git commit -m "$(cat <<'EOF'
Document thin roster refs as Manifest joins, not full agent definitions.

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Thin persist fields only | 3 |
| Join-at-read persona | 1, 6 |
| Fat PUT → 400 | 5 |
| Enriched GET/list | 5 |
| Eager migrate + stamp `manifest_id` | 2, 3 |
| LLM save → Manifest then thin | 5 |
| From-registry thin only | 5 |
| Temporal resolve at run | 6 (pipeline/activities share `_run_agent`) |
| Frontend minimal | 7 |
| Tests updated | 1–7 |
| Docs / docstring SoT | 8 |
| No prompt-binding / no PUT→Manifest proxy | Global constraints |

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-07-thin-roster-refs.md`. Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks  

**2. Inline Execution** — execute tasks in this session with executing-plans checkpoints  

Which approach?
