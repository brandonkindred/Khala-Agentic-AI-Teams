# Stage-2 binding verification (after save)

This is the reproducible evidence that a **saved** Studio / generated-agent
manifest drives a **Stage-2** run — the user-visible promise of the runtime-binding
work. It is both an automated suite and a manual checklist, so the result can be
reproduced by a human and stays valid as a regression guard now that the runtime
is bound.

The precedence contract these checks encode is
[`ADR-015`](../../../../../system_design/adr/ADR-015-invoke-generated-agent-persona-state-precedence.md).

## The three Stage-2 invoke paths

A single shared entrypoint (`…runtime.agent_builder:invoke_generated_agent`) is
stamped onto every saved Studio manifest and every generated-team manifest, so all
three Stage-2 paths run the *same* saved agent:

| Path | Persona source today | Binds saved persona? |
|---|---|---|
| Pipeline runner (`pipeline_runner._run_agent`) | `resolve_persona(manifest_id)` | **Yes** — bound today |
| Studio test-chat (`api/services/testing.send_test_chat_message`) | `resolve_persona(manifest_id)` | **Yes** — bound today |
| Sandbox invoke (`POST /_agents/{id}/invoke` → dispatch → entrypoint) | resolved `AgentManifest` (default); explicit request field overrides per invoke (ADR-015) | **Yes** — bound today (ADR-015) |

All three paths resolve `role` / `skills` / `expertise` (and, on the sandbox
invoke path, `system_prompt` / state) from the registered manifest, so a saved
persona drives every Stage-2 run. The sandbox invoke path additionally accepts a
per-invoke request override: a field explicitly present in the request body wins
over the manifest default for that invoke only, never written back.

## Automated verification

`tests/test_stage2_binding_after_save.py` starts every case from a *real* Stage-1
save (`build_studio_agent_manifest` / `build_agent_manifest` →
`AgentRegistry.register`) into a live in-process registry, then drives one Stage-2
path. The LLM is faked (records the composed system prompt / granted tools); no
network call and no Postgres are required. The sandbox invoke path is exercised
against both manifest producers — a Studio-saved manifest and an agentic-generated
one — to prove the binding is entrypoint-level, not producer-specific: both share
the identical save → shim → dispatch → entrypoint path.

```bash
cd backend
.venv/bin/pytest \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_stage2_binding_after_save.py \
  -v -rxX
```

Expected results:

| Test | Result | What it proves |
|---|---|---|
| `…_saved_studio_agent_is_resolvable_with_shared_entrypoint` | PASS | Save registers a resolvable manifest carrying the shared entrypoint. |
| `…_pipeline_stage2_binds_saved_persona_after_save` | PASS | Pipeline run is driven by the saved `role`/`skills`/`expertise`. |
| `…_test_chat_stage2_binds_saved_persona_after_save` | PASS | Test-chat run is driven by the saved persona. |
| `…_sandbox_invoke_stage2_binds_saved_persona_after_save` | PASS | Sandbox invoke (Studio-saved producer) is driven by the saved `role` and authored `executing`-state prompt. |
| `…_sandbox_invoke_stage2_honors_explicit_body_override` | PASS | Save → shim → dispatch → entrypoint wiring works; explicit body persona is honored. |
| `…_sandbox_invoke_stage2_never_grants_tools` | PASS | Runtime tools stay inert even for a manifest advertising `python`/`http_request`. |
| `…_sandbox_invoke_stage2_binds_saved_persona_after_save_for_generated_agent` | PASS | Sandbox invoke (agentic-generated producer) is driven by the saved `role`/`skills`/`expertise` — same path as the Studio-saved case. |
| `…_sandbox_invoke_stage2_honors_explicit_body_override_for_generated_agent` | PASS | Override precedence holds for the agentic-generated producer too. |
| `…_sandbox_invoke_stage2_never_grants_tools_for_generated_agent` | PASS | Runtime tools stay inert for the agentic-generated producer too. |

### Sandbox invoke: shim-level vs. real container boot

The sandbox-invoke cases above exercise the shared invoke shim
(`mount_invoke_shim`) mounted on a bare `FastAPI()` app — they prove the
save → shim → dispatch → entrypoint wiring, but not the actual container
process. `agent_sandbox_runtime/entrypoint.py` — the real Docker `CMD` for a
per-agent sandbox — has its own bootstrap step
(`_maybe_register_injected_manifest`) that registers a provision-time-injected
manifest (a Studio save or a generated agent, absent from the image's on-disk
registry) before the single-agent guard middleware and the shim ever see a
request. `agent_sandbox_runtime/tests/test_entrypoint.py` closes that second
layer: it injects a manifest carrying the real generated-agent entrypoint, boots
the real `_build_app()`, and posts an invoke through it.

`agent_sandbox_runtime` is platform infrastructure that stays team-agnostic — it
only imports `agent_platform` / `shared.*` in its own source, never a domain app
— so these cases build their manifests from `agent_platform.studio` (Studio
producer) and from `shared.manifests` primitives directly (generated producer;
the same primitives `agentic_team_provisioning.manifest_generation.build_agent_manifest`
itself delegates to) rather than importing that domain builder. The one
unavoidable domain-app reference — faking the Strands call inside
`agent_builder.py` so the test makes no real network call — is patched by
*string* target (`monkeypatch.setattr("agent_team_studio...agent_builder.StrandsAgent", ...)`)
rather than a top-level import, matching how production itself only ever
resolves a manifest's `source.entrypoint` string dynamically at dispatch time.

```bash
cd backend
.venv/bin/pytest agent_sandbox_runtime/tests/test_entrypoint.py -v -rxX
```

| Test | Result | What it proves |
|---|---|---|
| `test_entrypoint_binds_saved_studio_persona_after_save` | PASS | A real container boot (`_build_app()`), not just the shim in isolation, dispatches an invoke through the saved Studio `role`/`system_prompt`. |
| `test_entrypoint_binds_saved_generated_persona_after_save` | PASS | Same container-boot binding holds for an agentic-generated-style producer. |

## Manual checklist (UI reproduction)

To reproduce the same result by hand against a running stack:

1. **Stage 1 — save.** In Agent Studio, author an agent with a distinctive `role`
   (e.g. *"Audits vendor contracts"*) and a distinctive `system_prompt` (e.g.
   *"Always cite the clause number."*). Save it; note the returned `agent_id`.
2. **Stage 2 — pipeline / test-chat.** Run the saved agent through a pipeline step
   or the Studio test-chat. Confirm the reply reflects the saved role (these paths
   are bound today).
3. **Stage 2 — sandbox invoke.** `POST /_agents/{agent_id}/invoke` with **only**
   `{"agent_name", "message"}` (omit `role` / `skills` / `system_prompt`). The run
   is driven by the saved role and the authored `executing`-state prompt without
   re-supplying them in the body.
4. **Override still works.** Repeat step 3 with an explicit `role` in the body and
   confirm that value is used — the manifest is the default, an explicit request
   field wins for that one invoke (never written back).

Recording steps 1–4 with the observed outputs is sufficient checklist-backed
evidence alongside the automated suite.

## Epic acceptance-criteria validation (close-out)

This section is the evidence artifact for closing out the runtime-binding epic
chain (grandparent epic: "Agent Studio: bind saved manifest persona at
generated-agent invoke time"; parent story: "Runtime binding (3/3): verify
Stage-2/pipeline and remove unbound caveat"). Each grandparent-epic acceptance
criterion is validated point by point against concrete evidence in this
checkout:

| Epic acceptance criterion | Evidence |
|---|---|
| Invoking a saved Studio agent without re-supplying persona fields uses the manifest's stored `system_prompt` / role / states | `tests/test_stage2_binding_after_save.py::test_sandbox_invoke_stage2_binds_saved_persona_after_save` (line 177) and `agent_sandbox_runtime/tests/test_entrypoint.py::test_entrypoint_binds_saved_studio_persona_after_save` (line 354) — both drive a real Stage-1 save into a real registry, then a Stage-2 invoke that omits persona fields, and assert the composed prompt reflects the saved role/`system_prompt`. |
| Override semantics (request body vs. manifest) are documented and tested | Documented in full per-field precedence table in `system_design/adr/ADR-015-invoke-generated-agent-persona-state-precedence.md` ("Accepted and fully implemented"). Tested per field in `tests/test_invoke_binding_contract.py`: `role` (lines 88, 101), `skills` (117, 128, 143), `expertise` (161, 172), `capabilities` (189), `system_prompt`/`state` (226, 242, 264) — every override-able field now has a dedicated manifest-default test and a dedicated body-override test. |
| Agentic generated agents gain the same binding behavior (no Studio-only fork) | A single shared resolver, `persona_from_manifest`/`resolve_persona` (`roster_resolve.py:40-83`), is used identically by all three invoke paths (sandbox invoke, pipeline runner, Studio test-chat) — see "The three Stage-2 invoke paths" above. Both producer types are explicitly exercised: `test_stage2_binding_after_save.py::test_sandbox_invoke_stage2_binds_saved_persona_after_save_for_generated_agent` (line 271) and `test_entrypoint.py::test_entrypoint_binds_saved_generated_persona_after_save` (line 395). |
| Stage 2 sandbox run after Stage 1 save demonstrates bound behavior in tests or verified manual checklist | Both: the automated suite above (shim-level and real-container-boot-level), and the "Manual checklist (UI reproduction)" section above. |
| The "advertised but unbound" caveat is removed from docs/code comments | `registration.py`'s module docstring, `agent_platform/studio/README.md`, and `backend/shared/manifests/README.md` all describe binding as landed, pointing at ADR-015; no "advertised but unbound" / "dishonest" wording remains anywhere in the codebase (verified by repo-wide grep). |

### Coverage evidence

The 90%-floor coverage gate in `backend/pyproject.toml` (`[tool.coverage.report]
fail_under = 90`) only actually runs, in CI, for `agent_platform.studio`
(`registration.py`) via the `agent_studio` matrix entry. `agentic_team_provisioning`
(`agent_builder.py`, `roster_resolve.py`, `pipeline_runner.py`, `testing.py`),
`shared.agent_invoke` (`shim.py`, `dispatch.py`), and `agent_sandbox_runtime`
(`entrypoint.py`) have no dedicated CI coverage job as of this writing — a
pre-existing gap in the CI matrix (see `.github/workflows/ci.yml`'s own comment
on why `agentic_team_provisioning` isn't in the matrix yet), not something this
issue is fixing. The numbers below were therefore measured directly, locally,
scoped to each package, rather than cited from CI:

| Package | Command | Result |
|---|---|---|
| `agent_team_studio.agentic_team_provisioning` | `pytest agent_team_studio/agentic_team_provisioning/tests/ -m "not integration" --cov=agent_team_studio.agentic_team_provisioning --cov-fail-under=90` | **95.58%** total; `runtime/agent_builder.py` 100%, `roster_resolve.py` 94%. 409 passed, 1 pre-existing failure unrelated to binding (`test_api_router_scaffold.py::test_extracted_teams_and_conversations_paths_are_mounted`, a route-mounting assertion — matches the CI workflow's own documented "pre-existing failures" note for this suite). |
| `shared.agent_invoke` | `pytest ../shared/agent_invoke/tests/ --cov=shared.agent_invoke --cov-fail-under=90` | **94.46%** total; `shim.py` 95%, `dispatch.py` 90%. 58 passed. |
| `agent_platform.studio` | `pytest agent_platform/studio/tests/ -m "not integration" --cov=agent_platform.studio --cov-fail-under=90` | **91.84%** total; `registration.py` 100%. 181 passed, 17 skipped (pre-existing Postgres-availability gates). |
| `agent_sandbox_runtime` | `pytest agent_sandbox_runtime/tests/ --cov=agent_sandbox_runtime --cov-fail-under=90` | **90.62%**; `entrypoint.py` 91%. 16 passed. |

All four packages clear the 90% floor. `registration.py` and `runtime/agent_builder.py`
— the two files this epic's runtime change actually touches — are both at 100%.
