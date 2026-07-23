# Design: Guard formatPct/formatRatio against negative decimals

Date: 2026-07-23

## Goal

Prevent `formatPct` and `formatRatio` from throwing an uncaught `RangeError`
when callers pass a negative `decimals` argument to `Number.prototype.toFixed`.

## Context

`user-interface/src/app/shared/number-format.ts` exposes three display helpers:

- `formatPct(value, decimals = 1)` — `value.toFixed(decimals) + '%'`
- `formatRatio(value, decimals = 2)` — `value.toFixed(decimals)`
- `formatUsd(value, decimals = 0)` — `toLocaleString` (unaffected)

`toFixed` throws `RangeError` for negative fraction digits. Current call sites
(strategy-lab formatters, paper-trading panel) only pass defaults or
non-negative values, so this is a defensive guard rather than a live crash
fix.

## Decisions

| Topic | Choice |
|---|---|
| Strategy | Clamp via `Math.max(0, decimals)` — never throw for negative `decimals` |
| Shared helper | Private `clampDecimals(decimals: number): number` used by both functions |
| Upper bound | Out of scope — do not clamp to 100 |
| `formatUsd` | Out of scope |
| Call sites / defaults | Unchanged |

## Behavior

| Call | Result |
|---|---|
| `formatPct(12.34, -1)` | `'12%'` |
| `formatRatio(1.5, -1)` | `'2'` (`toFixed(0)` rounding) |
| Existing non-negative / default calls | Unchanged |

## Implementation

1. Add `function clampDecimals(decimals: number): number { return Math.max(0, decimals); }`
   in `number-format.ts` (module-private, not exported).
2. Use `value.toFixed(clampDecimals(decimals))` in `formatPct` and `formatRatio`.
3. Extend `number-format.spec.ts` with one negative-`decimals` case per function
   asserting the clamped output above.

## Testing / verification

- Unit tests cover negative `decimals` for both functions (clamp behavior).
- Existing cases remain green.
- `ng lint` clean; 90% coverage floor maintained for the touched file.

## Out of scope

- Upper-bound clamping for `decimals`
- Changes to `formatUsd`
- Changes to call sites or default `decimals` values
