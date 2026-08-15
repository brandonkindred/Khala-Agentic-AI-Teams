# Stage-2 binding verification (after save)

This is the reproducible evidence that a **saved** Studio / generated-agent
manifest drives a **Stage-2** run — the user-visible promise of the runtime-binding
work. It is both an automated suite and a manual checklist, so the result can be
reproduced by a human and stays valid as the runtime moves from *unbound* to *bound*.

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
| Sandbox invoke (`POST /_agents/{id}/invoke` → dispatch → entrypoint) | request body | **No** — unbound until manifest-first binding lands |

The pipeline and test-chat paths resolve `role` / `skills` / `expertise` straight
from the registered manifest, so a saved persona already drives those runs. The
sandbox invoke path still composes persona from the caller-supplied request body and
never reads the resolved manifest — the gap the binding implementation closes.

## Automated verification

`tests/test_stage2_binding_after_save.py` starts every case from a *real* Stage-1
save (`build_studio_agent_manifest` / `build_agent_manifest` →
`AgentRegistry.register`) into a live in-process registry, then drives one Stage-2
path. The LLM is faked (records the composed system prompt / granted tools); no
network call and no Postgres are required.

```bash
cd backend
.venv/bin/pytest \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_stage2_binding_after_save.py \
  -v -rxX
```

Expected on today's **unbound** runtime:

| Test | Result | What it proves |
|---|---|---|
| `…_saved_studio_agent_is_resolvable_with_shared_entrypoint` | PASS | Save registers a resolvable manifest carrying the shared entrypoint. |
| `…_pipeline_stage2_binds_saved_persona_after_save` | PASS | Pipeline run is driven by the saved `role`/`skills`/`expertise`. |
| `…_test_chat_stage2_binds_saved_persona_after_save` | PASS | Test-chat run is driven by the saved persona. |
| `…_sandbox_invoke_stage2_binds_saved_persona_after_save` | **XFAIL** | Sandbox invoke does **not** yet bind the saved persona (unbound). |
| `…_sandbox_invoke_stage2_honors_explicit_body_override` | PASS | Save → shim → dispatch → entrypoint wiring works; explicit body persona is honored. |
| `…_sandbox_invoke_stage2_never_grants_tools` | PASS | Runtime tools stay inert even for a manifest advertising `python`/`http_request`. |

### The unbound-vs-bound signal

The single `XFAIL` is the signal, not a silent gap:

- **Unbound (today):** the sandbox binding case reports **XFAIL** — an *expected*
  failure whose reason states persona is still taken from the request body. If it
  ever reports a plain **FAIL** (not XFAIL) or **ERROR**, a Stage-2 path regressed.
- **Bound (once manifest-first binding lands):** the same case starts passing and
  pytest reports it as **XPASS**. That flip is the proof the promise is now kept;
  when it happens, remove the `_SANDBOX_UNBOUND` marker so the case becomes a plain
  green regression guard.

## Manual checklist (UI reproduction)

To reproduce the same result by hand against a running stack:

1. **Stage 1 — save.** In Agent Studio, author an agent with a distinctive `role`
   (e.g. *"Audits vendor contracts"*) and a distinctive `system_prompt` (e.g.
   *"Always cite the clause number."*). Save it; note the returned `agent_id`.
2. **Stage 2 — pipeline / test-chat.** Run the saved agent through a pipeline step
   or the Studio test-chat. Confirm the reply reflects the saved role (these paths
   are bound today).
3. **Stage 2 — sandbox invoke.** `POST /api/agents/{agent_id}/invoke` with **only**
   `{"agent_name", "message"}` (omit `role` / `skills` / `system_prompt`).
   - **Unbound (today):** the run uses an empty/near-empty persona — the saved role
     and authored prompt do **not** appear. This matches the automated `XFAIL`.
   - **Bound (target):** the run is driven by the saved role and the authored
     `executing`-state prompt without re-supplying them in the body.
4. **Override still works.** Repeat step 3 with an explicit `role` in the body and
   confirm that value is used — the manifest is the default, an explicit request
   field wins for that one invoke (never written back).

Recording steps 1–4 with the observed outputs is sufficient checklist-backed
evidence alongside the automated suite.
