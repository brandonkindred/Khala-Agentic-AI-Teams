# Strategy-Card Label/Id Helper Unit Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add direct unit tests for `cardToggleLabel()`, `cardRegionLabel()`, and `cardBodyId()` on `StrategyCardComponent`.

**Architecture:** Tests-only change. Call the three helpers on the component instance inside a new `describe` next to the existing helper describes (`truncatedHypothesis`, `strategyCode`). Production methods already implement the contracts; no production edits.

**Tech Stack:** Angular 19, TypeScript, Vitest, existing `StrategyCardComponent` test harness in `strategy-card.component.spec.ts`.

**Spec:** `docs/superpowers/specs/2026-07-24-strategy-card-label-id-helper-tests-design.md`

## Global Constraints

- Tests only — do not change `strategy-card.component.ts` / `.html` / `.scss`.
- `cardToggleLabel` coverage: collapsed/`Show` only (no expanded/`Hide` case).
- Default fixture only (`asset_class: 'stocks'`, `lab_record_id: 'rec-1'`).
- Keep the existing DOM `aria-label` assertion under `showTitle / expanded gating`.
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body only).
- `docs/superpowers/` is gitignored — force-add plan/spec only when committing those docs on this branch; remove before merge if following prior branch cleanup pattern.

## File map

| Path | Responsibility |
|---|---|
| `user-interface/src/app/components/strategy-lab/strategy-card/strategy-card.component.spec.ts` | Add direct unit tests for the three helpers |

---

### Task 1: Direct unit tests for label/id helpers

**Files:**
- Modify: `user-interface/src/app/components/strategy-lab/strategy-card/strategy-card.component.spec.ts` (insert after the `strategyCode` describe, before the `verdictColor` test — currently around the blank line after line 265)
- Test: same file (run via Vitest)

**Interfaces:**
- Consumes: existing `beforeEach` that sets `component.record = makeRecord()` with `lab_record_id: 'rec-1'`, `strategy.asset_class: 'stocks'`, and default `expanded === false`
- Produces: three new passing `it(...)` cases under `describe('cardToggleLabel / cardRegionLabel / cardBodyId', ...)`

- [ ] **Step 1: Insert the new describe block**

After the closing `});` of `describe('strategyCode', ...)`, insert:

```typescript
  describe('cardToggleLabel / cardRegionLabel / cardBodyId', () => {
    it('cardToggleLabel includes Show verb and asset class when collapsed', () => {
      expect(component.expanded).toBe(false);
      expect(component.cardToggleLabel()).toBe('Show details for stocks strategy');
    });

    it('cardRegionLabel returns the asset-class details label', () => {
      expect(component.cardRegionLabel()).toBe('stocks strategy details');
    });

    it('cardBodyId uses the card-body-{lab_record_id} format', () => {
      expect(component.cardBodyId()).toBe('card-body-rec-1');
    });
  });
```

Note: production helpers already exist and match these strings — these tests are expected to pass on first run (coverage gap, not a red→green TDD cycle).

- [ ] **Step 2: Run the affected spec**

```bash
cd user-interface && npx vitest run src/app/components/strategy-lab/strategy-card/strategy-card.component.spec.ts
```

Expected: `40 passed` (37 existing + 3 new), exit code 0.

- [ ] **Step 3: Commit**

```bash
git add user-interface/src/app/components/strategy-lab/strategy-card/strategy-card.component.spec.ts
git commit -m "$(cat <<'EOF'
Add direct unit tests for strategy-card label and id helpers.

EOF
)"
```

---

## Plan self-review

| Spec requirement | Task coverage |
|---|---|
| Direct `cardToggleLabel` test (verb + asset class, collapsed only) | Task 1 Step 1 first `it` |
| Direct `cardRegionLabel` test | Task 1 Step 1 second `it` |
| Direct `cardBodyId` format test | Task 1 Step 1 third `it` |
| Vitest passes for affected spec | Task 1 Step 2 |
| No production changes / 90% floor unaffected | Global Constraints + File map |

No placeholders. Single file, single task.
