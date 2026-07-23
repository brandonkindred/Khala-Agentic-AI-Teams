# formatPct/formatRatio Negative-Decimals Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clamp negative `decimals` in `formatPct` and `formatRatio` so `toFixed` never throws `RangeError`.

**Architecture:** Add a module-private `clampDecimals(decimals: number): number` that returns `Math.max(0, decimals)`. Both formatters pass `decimals` through it before calling `toFixed`. No public API changes; `formatUsd` untouched.

**Tech Stack:** TypeScript, Angular 19 UI package, Vitest (via `npm test` / `npx vitest`).

**Spec:** `docs/superpowers/specs/2026-07-23-format-decimals-guard-design.md`

## Global Constraints

- Strategy is clamp (`Math.max(0, decimals)`), not throw
- Do not clamp an upper bound
- Do not modify `formatUsd` or any call sites
- Keep existing defaults (`formatPct` → 1, `formatRatio` → 2)
- Maintain 90% line coverage for the touched file; `ng lint` clean
- Work in worktree `.worktrees/issue-2110-format-decimals-guard` on branch `fix/2110-format-decimals-guard`

## File Structure

| File | Role |
|---|---|
| `user-interface/src/app/shared/number-format.ts` | Add `clampDecimals`; wire into `formatPct` / `formatRatio` |
| `user-interface/src/app/shared/number-format.spec.ts` | Add negative-`decimals` cases for both functions |

---

### Task 1: Failing tests for negative decimals

**Files:**
- Modify: `user-interface/src/app/shared/number-format.spec.ts`
- Test: `user-interface/src/app/shared/number-format.spec.ts`

**Interfaces:**
- Consumes: existing `formatPct(value: number, decimals?: number): string`, `formatRatio(value: number, decimals?: number): string`
- Produces: two new Vitest cases asserting clamp-to-zero behavior

- [ ] **Step 1: Ensure frontend deps are available**

From worktree root:

```bash
cd user-interface
if [ ! -d node_modules ]; then
  # Prefer linking the main checkout's install when present to avoid a full npm ci
  if [ -d ../../user-interface/node_modules ]; then
    ln -s ../../user-interface/node_modules node_modules
  else
    npm ci
  fi
fi
```

- [ ] **Step 2: Write the failing tests**

Append these cases inside the existing `describe('formatPct', …)` and `describe('formatRatio', …)` blocks in `user-interface/src/app/shared/number-format.spec.ts`:

```typescript
  it('clamps a negative decimals count to zero', () => {
    expect(formatPct(12.34, -1)).toBe('12%');
  });
```

```typescript
  it('clamps a negative decimals count to zero', () => {
    expect(formatRatio(1.5, -1)).toBe('2');
  });
```

Place each as the last `it` in its `describe` block. Do not change existing tests.

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd user-interface
npx vitest run src/app/shared/number-format.spec.ts
```

Expected: FAIL — both new cases throw `RangeError` from `toFixed` (or equivalent failure), not assertion mismatches on clamped strings.

- [ ] **Step 4: Commit the failing tests**

```bash
git add user-interface/src/app/shared/number-format.spec.ts
git commit -m "$(cat <<'EOF'
Add failing tests for negative decimals in formatPct/formatRatio.

EOF
)"
```

---

### Task 2: Implement clampDecimals and make tests pass

**Files:**
- Modify: `user-interface/src/app/shared/number-format.ts`
- Test: `user-interface/src/app/shared/number-format.spec.ts`

**Interfaces:**
- Consumes: Task 1 failing expectations
- Produces: `function clampDecimals(decimals: number): number` (module-private); `formatPct` / `formatRatio` use it

- [ ] **Step 1: Implement clamp and wire formatters**

Replace the contents of `user-interface/src/app/shared/number-format.ts` so the `toFixed` helpers look like this (`formatUsd` unchanged):

```typescript
/** Clamp fraction digits to a non-negative count safe for `Number.prototype.toFixed`. */
function clampDecimals(decimals: number): number {
  return Math.max(0, decimals);
}

/** Format a percentage value as a string, e.g. `formatPct(12.34)` → `'12.3%'`. */
export function formatPct(value: number, decimals = 1): string {
  return value.toFixed(clampDecimals(decimals)) + '%';
}

/** Format a plain ratio value (Sharpe, profit factor, …) to a fixed number of decimals. */
export function formatRatio(value: number, decimals = 2): string {
  return value.toFixed(clampDecimals(decimals));
}

/**
 * Format a USD amount with a leading $ and thousands separators, e.g.
 * `formatUsd(150000)` → `'$150,000'`. A negative amount renders as `'-$150,000'`
 * (sign before the currency symbol, standard US convention) rather than
 * `'$-150,000'`.
 */
export function formatUsd(value: number, decimals = 0): string {
  const sign = value < 0 ? '-' : '';
  return (
    sign + '$' + Math.abs(value).toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
  );
}
```

- [ ] **Step 2: Run unit tests to verify they pass**

```bash
cd user-interface
npx vitest run src/app/shared/number-format.spec.ts
```

Expected: PASS — all cases green, including the two new clamp cases.

- [ ] **Step 3: Run lint**

```bash
cd user-interface
npx ng lint
```

Expected: clean (exit 0), no new findings in `number-format.ts` / `.spec.ts`.

- [ ] **Step 4: Commit the implementation**

```bash
git add user-interface/src/app/shared/number-format.ts
git commit -m "$(cat <<'EOF'
Clamp negative decimals in formatPct and formatRatio.

EOF
)"
```

---

## Spec coverage (self-review)

| Spec requirement | Task |
|---|---|
| Clamp via `Math.max(0, decimals)` | Task 2 |
| Shared private `clampDecimals` | Task 2 |
| Both `formatPct` and `formatRatio` | Task 2 |
| Unit tests for negative decimals | Task 1 |
| `formatUsd` / call sites / upper bound out of scope | Honored (not in any task) |
| `ng lint` + coverage floor | Task 2 Step 3; coverage maintained because new lines are exercised by new tests |
