# Design: Strategy-card label/id helper unit tests

Date: 2026-07-24

## Goal

Add direct unit-test coverage for three accessibility-facing helpers on `StrategyCardComponent` so regressions in ARIA label/id strings fail the suite immediately.

## Context

`cardToggleLabel()`, `cardRegionLabel()`, and `cardBodyId()` drive the disclosure toggle’s `aria-label`, the expanded region’s `aria-label`, and the shared `id` / `aria-controls` pairing. The existing spec only exercises `cardRegionLabel` indirectly via a DOM `aria-label` assertion; the other two helpers are untested. Helper methods elsewhere in this file (`truncatedHypothesis`, `strategyCode`) are already covered with direct method-call describes.

## Decisions

| Topic | Choice |
|---|---|
| Approach | Direct method-call tests on the component instance |
| File | `user-interface/src/app/components/strategy-lab/strategy-card/strategy-card.component.spec.ts` only |
| Production code | Unchanged |
| `cardToggleLabel` states | Collapsed only (`Show …`); no expanded/`Hide` case |
| Asset-class variants | Default fixture (`stocks`) only |
| Existing DOM assertion | Keep the expanded-region `aria-label` DOM check as-is |
| DOM wiring (`aria-controls`) | Out of scope |

## Test design

Place a new `describe` near the other helper describes. Default `beforeEach` fixture already provides `lab_record_id: 'rec-1'`, `asset_class: 'stocks'`, and `expanded === false`.

| Method | Assertion |
|---|---|
| `cardToggleLabel()` | `'Show details for stocks strategy'` |
| `cardRegionLabel()` | `'stocks strategy details'` |
| `cardBodyId()` | `'card-body-rec-1'` |

Three `it(...)` blocks; exact string equality (not substring checks).

## Out of scope

- Behavior or implementation changes to the three helpers
- Expanded-state (`Hide`) toggle label coverage
- Additional asset-class permutations
- Template wiring assertions beyond what already exists

## Verification

- `npx vitest run src/app/components/strategy-lab/strategy-card/strategy-card.component.spec.ts` → 40 passing (37 existing + 3 new)
- Production `src/app` coverage floor unchanged (tests-only change)
