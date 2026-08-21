# Cognition HITL Review Panel — Implementation Plan

> **Note:** This plan was written when Cognition was a tab inside the Agent Console.
> The Agent Console shell has since been deleted; Cognition now lives at its own
> route (`/cognition`) as a standalone page. References to `agent-console.component`
> below are historical only.

Implementation plan for the ~~Agent Console **Cognition** tab~~ standalone Cognition page. This plan is the
build companion to [`DESIGN.md`](./DESIGN.md) and incorporates every design
requirement from it (section references below point at `DESIGN.md §N`).

**Scope:** frontend-only. The `/api/cognition` backend (memory events/summaries,
rules, proposals approve/reject) already exists; this work consumes it.

**Acceptance (issue #740):** approve/reject round-trips against the API;
≥90% vitest line coverage.

---

## 1. Files

### New

| File | Responsibility | Design ref |
|---|---|---|
| `services/cognition-api.service.ts` | HttpClient calls over `environment.agentCognitionApiUrl` | §2, §11 |
| `services/cognition-api.service.spec.ts` | Service tests (`HttpClientTestingModule`) | §11 testing |
| `models/cognition.model.ts` | TS interfaces + enums mirroring backend Pydantic models | §11 |
| `models/cognition-labels.ts` | Pure backend-value → UI-copy mapping (the §8 glossary) | §8 |
| `models/cognition-labels.spec.ts` | Unit tests for every glossary mapping | §8 |
| `components/cognition-page/cognition-tab/cognition-tab.component.{ts,html,scss}` | The tab: picker + 3 sections | §3–§7 |
| `components/cognition-page/cognition-tab/cognition-tab.component.spec.ts` | Component tests (mocked service) | §11 testing |

### Modified

| File | Change | Design ref |
|---|---|---|
| `environments/environment.ts` + `environment.prod.ts` | Add `agentCognitionApiUrl: \`${apiBase}/api/cognition\`` | §2 |
| ~~`components/agent-console/agent-console.component.ts`~~ | ~~Import `CognitionTabComponent`, add to `imports[]`~~ (removed — Console shell deleted; Cognition is now a standalone route) | §1 |
| ~~`components/agent-console/agent-console.component.html`~~ | ~~Add 7th `<mat-tab>`~~ (removed — Console shell deleted; Cognition lives at `/cognition`) | §1 |

---

## 2. Models — `cognition.model.ts` (§11)

Mirror the backend exactly so responses deserialize 1:1:

- Enums (string unions): `EventKind` (`observation|action|tool_call|outcome|error|feedback`),
  `Scale` (`day|week|month|year`), `RuleMode` (`advisory|enforced`),
  `RuleStatus` (`active|retired`), `RuleSource` (`seed|derived|operator`),
  `ProposalAction` (`add|amend|retire`), `ProposalStatus`
  (`pending|approved|rejected|superseded`).
- Interfaces: `MemoryEvent`, `PeriodSummary`, `Rule`, `RuleProposal` with the
  fields listed in `DESIGN.md §11`.

DbC: each interface documents its invariants (e.g. `RuleProposal` action
coherence — `add`⇒`proposed_rule` set, `retire`⇒`target_rule_id` set,
`amend`⇒both).

## 3. Copy mapping — `cognition-labels.ts` (§8 — load-bearing)

The UI must **never** render raw enums/ids. Centralize the §8 glossary as pure
functions (no I/O), unit-tested exhaustively:

- `eventKindLabel(kind)` → lowercase plain label (`tool_call` → `tool call`).
- `relevanceLabel(salience)` → `relevance 0.82`.
- `memoryOrderLabel(bySalience)` → `Most relevant` / `Most recent`.
- `proposalActionLabel(action)` → `new rule` / `amend rule` / `retire rule`.
- `ruleSourceLabel(source)` → `built-in` / `learned` / `added by you`.
- `rulePriorityLabel(priority)` → `priority 90`.
- `ruleModeTooltip(mode)` → *enforced: blocks the agent* / *advisory: guidance only*.
- `EVIDENCE_OUTDATED = 'evidence outdated'` — the single shared term for
  `stale_evidence` (proposals) **and** `needs_review` (rules).

All chips render **lowercase**; section titles and buttons keep normal casing.

## 4. API service — `cognition-api.service.ts` (§2)

`@Injectable({providedIn:'root'})`, `inject(HttpClient)`, `baseUrl =
environment.agentCognitionApiUrl`. Methods (all `agent_id`-scoped, `HttpParams`
for query, `encodeURIComponent` on path segments):

- `listProposals(agentId, {status?, limit?, offset?})` → `Observable<RuleProposal[]>`
- `approveProposal(agentId, proposalId)` → `Observable<Rule>`
- `rejectProposal(agentId, proposalId)` → `Observable<RuleProposal>`
- `listMemoryEvents(agentId, {topN?, bySalience?, since?})` → `Observable<MemoryEvent[]>`
- `listSummaries(agentId, scale, {limit?, offset?, excludeStale?})` → `Observable<PeriodSummary[]>` *(optional disclosure)*
- `listRules(agentId, {status?, limit?, offset?})` → `Observable<Rule[]>`

Errors propagate; the component maps `err.error.detail` (§4, §7).

## 5. Component — `cognition-tab.component.ts` (§3–§7)

Standalone, `ChangeDetectionStrategy.OnPush`, signal-based state. Reuses shared
`app-loading-spinner` / `app-empty-state` / `app-error-message`.

**Agent picker (§3):** inject `AgentCatalogApiService` (the existing
catalogue), load agents on init, auto-select the first; `agentId` is a signal.
Changing it (or `⟳`) clears section state and refetches **proposals + memory +
rules**.

**Section order (§2): Proposals first, then Memory, then Rules.** Per-section
signals: `items`, `loading`, `error`, plus filter signals.

**Proposals (§4):**
- Filter signal (default `pending`). Cards render via `cognition-labels`
  (`new rule`/`amend rule`/`retire rule`, lowercase chips).
- `amend` shows old→new rule text ("Replaces:" / "With:"); `retire` shows the
  target rule's text, not its id.
- `stale_evidence` ⇒ `⚠ evidence outdated` chip + **disabled** Approve with
  tooltip "Can't approve while evidence is outdated."
- Approve/Reject guarded by `app-confirm-dialog`. **Optimistic update with
  rollback on error** (Backlog-tab pattern). On approve success, refetch Rules
  so the activated rule appears; on reject, move the card to the rejected filter.

**Memory (§5):** order toggle `Most relevant`/`Most recent` (→ `by_salience`),
`top_n` selector (25/50/100), kind chips + `relevance N.NN` via labels. Optional
`mat-expansion-panel` summaries with a `scale` selector (deferred-friendly).

**Rules (§6):** status filter (`all|active|retired`), highest priority first,
read-only. Rows show mode chip (+tooltip), status, `priority N`, source label,
`⚠ evidence outdated` when `needs_review`, and `why: …` for rationale. Retired
rows dimmed + struck-through.

**States (§7):** loading/empty/error per section with the exact microcopy in
§7; `503` surfaced panel-wide as "Cognition data is temporarily unavailable.
Try again shortly."

## 6. Styling & a11y (§9, §10)

- SCSS uses `--kh-*` tokens only; BEM classes `.cognition-tab`,
  `.proposal-card`, `.memory-event`, `.rule-row` with `.is-pending/-stale/-retired`.
- Regions are `aria-labelled`; status/kind/mode conveyed by icon **+** text;
  disabled Approve has `aria-describedby` → the outdated-evidence note;
  `enforced`/`advisory` tooltips exposed to screen readers.

## 7. Testing (§11 — ≥90% line coverage, hard CI floor)

- **`cognition-labels.spec.ts`** — exhaustive: every enum value → expected label
  (cheap, high-coverage, locks the glossary).
- **`cognition-api.service.spec.ts`** — `HttpClientTestingController.expectOne`
  asserting URL/method/params per method; `flush` success + error bodies; cover
  the `409` (evidence outdated / not pending) and `503` paths; `verify()` in
  `afterEach`.
- **`cognition-tab.component.spec.ts`** — mock `CognitionApiService` +
  `AgentCatalogApiService` with `vi.fn().mockReturnValue(of(...))`: auto-select
  first agent; proposals render first; approve round-trip (optimistic + Rules
  refetch); reject; disabled Approve when `stale_evidence`; filter changes
  refetch; error rendering; empty states.
- Run `npm run test:coverage`; justify any unavoidable `istanbul ignore` inline.

## 8. Build order (checklist)

1. [x] `environment.{ts,prod.ts}` — add `agentCognitionApiUrl`.
2. [x] `cognition.model.ts` + `cognition-labels.ts` (+ spec).
3. [x] `cognition-api.service.ts` (+ spec).
4. [x] `cognition-tab.component.*` — picker → Proposals → Memory → Rules (+ spec).
5. [x] ~~Wire 7th tab into `agent-console.component.{ts,html}`.~~ (Console shell deleted; Cognition is now a standalone route at `/cognition`.)
6. [x] Lint + production build green; 100% line coverage on the new files
   (37 specs: labels 9, service 12, component 16).
7. [ ] Manual smoke: approve/reject round-trip against a running unified API.

## 9. Out of scope / follow-ups

- Period **summaries** disclosure (§5) may ship in a fast-follow if it risks the
  coverage gate; the events timeline is the must-have.
- No backend changes; no new routes. Agent picker intentionally reuses the
  existing catalogue rather than adding a "cognition-enabled agents" endpoint.
