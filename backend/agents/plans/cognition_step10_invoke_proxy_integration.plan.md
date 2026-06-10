---
name: cognition-step10-invoke-proxy-integration
overview: Make the cognition inject-on-invoke / writeback-on-return contract live at the invoke boundary (Agent Cognition IMPLEMENTATION_PLAN.md, Step 10). Wire the run-ledger idempotency machinery (claim/replay/conflict/reclaim), the Idempotency-Key contract, lazy rollup catch-up, the enforced precondition gate with blocked-envelope storage, full-envelope payload re-cap, postcondition-before-persist ordering, trusted-audit persistence on violation, writeback persistence on success, and an in-process helper so teams without an HTTP hop get the same lifecycle.
todos:
  - id: invoke-gate-module
    content: "Create backend/agents/agent_cognition/invoke_gate.py: the reusable invoke lifecycle orchestrator over the Step 8 facade (context.py). Exposes derive_source_run_id(body, idempotency_key) -> (source_run_id, request_hash), prepare_invoke(agent_id, manifest_cognition, body, idempotency_key) -> PreparedInvoke | ReplayResult | ConflictResult | BlockedResult, and finalize_invoke(prepared, upstream_status, envelope) -> FinalizedResult. Pure orchestration: no FastAPI imports, raises typed results instead of HTTPException so both the HTTP route and in-process callers can map them. Full DbC docstrings (Preconditions/Postconditions/Invariants)."
    status: pending
  - id: request-hash
    content: "request_hash = sha256 hexdigest of the canonical JSON of the caller's body (json.dumps(body, sort_keys=True, separators=(',', ':'), default=str)). source_run_id = caller Idempotency-Key header when present, else the request_hash (byte-identical keyless retries still dedup). Unit-test: same body -> same hash regardless of key order; different body -> different hash."
    status: pending
  - id: requires-idempotency-key-400
    content: "In prepare_invoke: when manifest cognition.requires_idempotency_key is true and no caller Idempotency-Key was supplied, reject 400 (side-effecting agent; without a key the call is at-least-once, not run-once). No sandbox is spun up and no ledger row is written."
    status: pending
  - id: ledger-claim
    content: "Claim the leased agent_cognition_runs ledger via context.claim_run(agent_id, source_run_id, request_hash, default_run_lease()). Map ClaimState: REPLAY -> return the stored envelope verbatim ({status_code, content}) without re-invoking (covers completed AND blocked rows); CONFLICT -> 409 (different body for the same key, or in_progress with a valid lease); CLAIMED -> proceed carrying the claim_token. Expired-lease reclaim (hash retained) is already inside claim_run — re-execution just proceeds on CLAIMED."
    status: pending
  - id: ledger-degradation
    content: "Storage-unavailable policy: when AgentCognitionStorageUnavailable is raised at claim time, a requires_idempotency_key agent fails 503 (run-once cannot be guaranteed for a side-effecting agent), while other cognition agents degrade to an unledgered invoke with a WARNING (matches the existing never-break-the-invoke posture of _maybe_build_cognition)."
    status: pending
  - id: lazy-catchup
    content: "Lazy rollup catch-up before context load: await asyncio.to_thread(context.ensure_rollups_current, agent_id, now) inside prepare_invoke, best-effort (log + continue on failure — a rollup hiccup must not break the invoke)."
    status: pending
  - id: load-context-via-facade
    content: "Replace the route's direct agent_cognition.invoke_context import with the Step 8 facade: context.load_context(agent_id, query=extract_query_text(body)). _maybe_build_cognition folds into prepare_invoke."
    status: pending
  - id: precondition-gate
    content: "Enforced precondition gate via context.enforce_precondition (raises PreconditionBlocked). On block: build the 4xx envelope {error: 'Blocked by cognition precondition', reason, phase: 'precondition'}; persist one memory event (kind=error, content carrying the reason, source_seq=0) through persist_writeback (which re-pins agent_id/source_run_id and sanitizes); complete_run(status='blocked', response={status_code: 422, content}, claim_token) so a retried block replays the same 4xx; return 422. Advisory rules are NOT rendered into prompts by the proxy — they travel in the cognition side channel for the runtime to render (per IMPLEMENTATION_PLAN.md Step 10; the runtime rendering ships with the generator scaffold in Step 14)."
    status: pending
  - id: envelope-recap
    content: "After wrap_request(body, cognition.model_dump(mode='json')), re-apply AGENT_INVOKE_MAX_PAYLOAD_BYTES to the serialized FULL envelope (body + cognition block) before posting to the sandbox -> 413 on overflow with a detail explaining the cognition overhead. The existing read_json_capped check only bounds the caller body; a near-cap request plus a large digest must not slip past the shim's own cap as a confusing downstream 413."
    status: pending
  - id: return-ordering
    content: "Reorder the return path: parse upstream envelope -> postcondition check FIRST (context.enforce_postcondition on the envelope's output when it is a dict) -> then persist. On pass: persist_writeback(agent_id, source_run_id, CognitionWriteback(events=validated memory_events)) where the shim envelope's memory_events dicts are validated into MemoryEvent models defensively (invalid entries skipped with a warning, never failing the invoke); then _persist_run (console history); then complete_run(status='completed', response={status_code, content}, claim_token); return. tool_calls are not separately persisted — the broker already emits tool_call/outcome events into memory_events (per the persist_writeback contract)."
    status: pending
  - id: postcondition-violation-path
    content: "On PostconditionViolation: drop the model output and the agent-authored memory_events; persist ONLY the shim's trusted tool_audit (converted to tool_call MemoryEvents) plus one blocked-run event (kind=error, phase=postcondition, reason); _persist_run with output_data=None and error set (the run row is audit, not a replayable result); complete_run(status='blocked', response={status_code: 422, content: violation envelope}, claim_token); return 422. Fixes the current ordering bug where _persist_run stores the full output before the postcondition gate runs."
    status: pending
  - id: replay-no-duplicate-run-rows
    content: "A REPLAY response does not write a second agent_console run row (the original invoke already recorded one) and does not call note_activity/acquire — no sandbox is touched. Response carries the stored status code and content unchanged."
    status: pending
  - id: route-rewire
    content: "Rework unified_api/routes/agents.py invoke_agent into a thin adapter over invoke_gate: read Idempotency-Key header, call prepare_invoke (mapping BlockedResult/ConflictResult/ReplayResult to 422/409/stored-status responses), keep the existing sandbox acquire/warming/timeout plumbing, post the wrapped envelope, call finalize_invoke on return. Non-cognition agents (manifest.cognition is None or Postgres off) keep today's exact pass-through behavior: no ledger, no envelope, no gates."
    status: pending
  - id: in-process-helper
    content: "In-process helper (no HTTP hop) in invoke_gate.py: async def invoke_in_process(agent_id, manifest_cognition, body, runner, *, idempotency_key=None) where runner(input, cognition_ctx) is the team's direct callable. Runs the identical lifecycle — derive key, claim/replay/conflict, catch-up, load context, precondition, run, postcondition, writeback, complete — returning the same envelope shape. Lives in agent_cognition (not unified_api) so teams can import it without a layering inversion."
    status: pending
  - id: platform-bound-seam
    content: "platform_bound tools: no new proxy code in this step. The SB-PX tool_calls/tool_results protocol entry point (drive_platform_bound_loop) landed with the tools layer and is exercised with a stubbed runtime; live use by generated agents is gated on the Step 14 runtime scaffold. This step only guarantees the writeback/audit reconciliation handles events from either loop, covered by a test feeding broker-shaped memory_events through finalize_invoke."
    status: pending
  - id: tests-route
    content: "Extend unified_api/tests/test_agents_route.py with a stubbed sandbox (monkeypatched acquire + httpx transport) and monkeypatched facade seams (claim_run/complete_run/replay via an in-memory fake ledger; load_context; persist_writeback; ensure_rollups_current). Acceptance matrix: (1) entrypoint receives only its declared input — posted body is the marker envelope with verbatim input; (2) retry same key+body replays without re-invoking (sandbox stub called once); (3) same key different body -> 409; (4) retried precondition block and retried postcondition block each replay the same 4xx from the ledger; (5) concurrent retry while leased -> 409; (6) expired-lease retry re-executes; (7) requires_idempotency_key agent without header -> 400, with header -> proceeds; (8) near-cap request + cognition -> 413 at the envelope re-cap; (9) near-cap output plus writeback does not drop memory (per-field caps pass through); (10) postcondition violation -> 422, output not persisted to console run or memory, trusted tool audit IS persisted; (11) storage outage: plain cognition agent degrades, requires_idempotency_key agent -> 503; (12) non-cognition agent path byte-identical to today."
    status: pending
  - id: tests-invoke-gate
    content: "Unit tests for invoke_gate.py against an in-memory fake of the context facade: source_run_id/request_hash derivation, every prepare_invoke branch, finalize ordering (postcondition evaluated before any persistence), defensive memory_events validation (malformed entry skipped), in-process helper end-to-end with a stub runner."
    status: pending
  - id: lint-coverage-docs
    content: "ruff clean (make lint); >=90% line coverage on all new/changed code; document the Idempotency-Key header + replay/409/400 semantics in backend/agents/agent_cognition/README.md and the agent-console invoke docs; add any new env knobs to docs/ENV_VARS.md (none expected — AGENT_COGNITION_RUN_LEASE_S already shipped with the facade)."
    status: pending
isProject: false
---

# Cognition Step 10 — Invoke proxy integration

Implementation plan for Step 10 of `backend/agents/agent_cognition/IMPLEMENTATION_PLAN.md`:
make the inject-on-invoke / writeback-on-return contract live at the invoke boundary.

## Where the code stands today

Everything Step 10 depends on is merged:

- **Facade (`agent_cognition/context.py`)** — `load_context`, `ensure_rollups_current`,
  `enforce_precondition` / `enforce_postcondition` (raise `PreconditionBlocked` /
  `PostconditionViolation`), `persist_writeback` (re-pins ids, strips secrets, bounds
  salience/content/timestamps), and the run ledger: `claim_run` (atomic
  claim / replay / conflict / expired-lease reclaim with `claim_token` fencing),
  `complete_run`, `replay_run`, `default_run_lease()`.
- **Envelope (`agent_cognition/tools/envelope.py`)** — `ENVELOPE_MARKER`, `wrap_request`,
  `try_unwrap_request`. The shim (`shared_agent_invoke/shim.py`) already unwraps on the
  marker only, runs the brokered tool loop in its own frame, applies per-field caps
  (`max_output_bytes` on `output`, `max_writeback_bytes` on audit+events), and returns
  `InvokeEnvelope{output, duration_ms, trace_id, logs_tail, error, truncated,
  timeout_hit, tool_audit, memory_events}`.
- **Manifest (`agent_registry/models.py`)** — optional `CognitionSpec` with
  `requires_idempotency_key: bool`.
- **Route (`unified_api/routes/agents.py`)** — already builds the cognition context,
  rejects caller bodies carrying the marker, runs inline pre/post predicate checks
  (422), wraps the request, and uses the summed per-field response cap.

## What is missing (the actual Step 10 work)

1. **No idempotency.** No `Idempotency-Key` handling, no `request_hash`, no
   `claim_run`/`complete_run` calls, no replay of terminal envelopes, no 409s, no 400
   for keyless side-effecting agents.
2. **No lazy catch-up.** `ensure_rollups_current` is never called on the invoke path.
3. **No writeback persistence.** The shim's `memory_events`/`tool_audit` are returned to
   the UI and dropped — nothing reaches the episodic store.
4. **Blocked runs aren't durable.** A precondition block 422s without a memory event or
   a `blocked` ledger row, so a retried block re-evaluates instead of replaying.
5. **Ordering bug.** `_persist_run` stores the full model output *before* the
   postcondition gate runs, so a violated output is persisted to run history — the
   acceptance requires output dropped, trusted tool audit kept.
6. **No full-envelope cap.** `AGENT_INVOKE_MAX_PAYLOAD_BYTES` bounds the caller body
   only; the posted envelope (body + rules + memory digest) can exceed it and fail
   confusingly at the shim.
7. **No in-process helper** for teams that call agents without the HTTP hop.
8. The route bypasses the facade (`invoke_context` + raw evaluators) instead of using
   the one seam Step 8 built.

## Design

### New module: `agent_cognition/invoke_gate.py`

One reusable orchestrator for the whole lifecycle, shared by the HTTP route and
in-process callers. Pure orchestration over the facade — no FastAPI types, returns
typed results the caller maps to transport:

```
derive_source_run_id(body, idempotency_key) -> (source_run_id, request_hash)
prepare_invoke(...)  -> PreparedInvoke | ReplayResult | ConflictResult | BlockedResult
finalize_invoke(...) -> FinalizedResult (completed | blocked)
invoke_in_process(agent_id, cognition_spec, body, runner, *, idempotency_key=None)
```

- `request_hash` = sha256 of canonical JSON (`sort_keys=True`, compact separators).
- `source_run_id` = caller `Idempotency-Key` if present, else `request_hash`.
- `PreparedInvoke` carries `source_run_id`, `claim_token`, the `CognitionContext`,
  and the wrapped envelope, so finalize can complete the same claim.
- Ledger envelopes are stored as `{"status_code": int, "content": {...}}` so a blocked
  replay reproduces the original 4xx and a completed replay the original 2xx.

The helper lives in `agent_cognition` (not `unified_api`) so backend teams can import
it without a layering inversion.

### Request path (HTTP route)

1. Read + cap caller body (unchanged); reject marker-carrying bodies (unchanged).
2. Cognition-enabled agent (manifest block present + Postgres) → `prepare_invoke`:
   - `requires_idempotency_key` and no header → **400** (at-least-once is not run-once).
   - `claim_run`: REPLAY → stored envelope, no sandbox touch, no duplicate console run
     row; CONFLICT → **409**; CLAIMED → continue.
   - Storage outage: side-effecting agent → **503**; otherwise degrade unledgered
     with a WARNING (matches the existing never-break-the-invoke posture).
   - Best-effort `ensure_rollups_current` (via `asyncio.to_thread`), then
     `load_context`.
   - `enforce_precondition`; on block → persist one `error` memory event via
     `persist_writeback`, `complete_run(status="blocked", response=422-envelope)`,
     return **422**. The proxy never edits prompts — advisory rules ride the
     `cognition` side channel for the runtime to render (Step 14).
3. `wrap_request`, then **re-apply `AGENT_INVOKE_MAX_PAYLOAD_BYTES` to the serialized
   full envelope** → 413 on overflow.
4. Sandbox acquire / warming / timeout / combined response cap: unchanged.

### Return path

1. Parse the upstream envelope.
2. **Postcondition first** (`enforce_postcondition` on the envelope's `output` dict).
3. **Pass:** validate `memory_events` dicts into `MemoryEvent` models defensively
   (skip malformed entries with a warning), `persist_writeback`, `_persist_run`,
   `complete_run("completed", {status_code, content})`, return upstream status.
   `tool_calls` are not separately persisted — the broker already emits
   `tool_call`/`outcome` events into `memory_events`.
4. **Violation:** drop model output and agent-authored events; persist only the shim's
   trusted `tool_audit` (as `tool_call` events) + one blocked-run `error` event;
   `_persist_run` with `output_data=None` and the violation as `error`;
   `complete_run("blocked", 422-envelope)`; return **422**.

Non-cognition agents keep today's behavior byte-for-byte: no ledger, no envelope,
no gates, `_persist_run` as-is.

### platform_bound tools

No new proxy code this step. `drive_platform_bound_loop` (the SB↔PX
`tool_calls`/`tool_results` protocol) shipped with the tools layer and is exercised
with a stubbed runtime; live use by generated agents is gated on the Step 14 runtime
scaffold. Step 10 only guarantees finalize handles broker-shaped events from either
loop — covered by a dedicated test.

## Test plan (stubbed sandbox; ≥90% coverage; ruff clean)

`unified_api/tests/test_agents_route.py` gains a cognition fixture set: a manifest
with a `cognition` block (one variant with `requires_idempotency_key: true`), a
monkeypatched `acquire` returning a WARM handle, an httpx transport stub capturing the
posted body, and an in-memory fake of the facade ledger. The acceptance matrix maps
1:1 to the issue's gate (see todo `tests-route`); `tests/test_invoke_gate.py` covers
the orchestrator branches and the in-process helper in isolation.

## Risk notes

- **Replay status fidelity** depends on storing `{status_code, content}` — review that
  the stored content excludes the per-invoke `sandbox` block or marks replays so the
  UI doesn't treat a replay as a fresh boot.
- **Degraded-mode semantics** (storage outage → unledgered invoke) trades strict
  run-once for availability on non-side-effecting agents; side-effecting agents get
  the strict 503. Called out in the README so operators aren't surprised.
- **`_persist_run` reorder** changes when console history is written on error paths;
  the existing 422-detail unwrapping in `_persist_run` must keep working for shim-level
  agent errors (which are unrelated to postcondition blocks).
