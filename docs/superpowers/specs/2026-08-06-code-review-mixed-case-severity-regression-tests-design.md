# Design: Expand mixed-case severity blocking regression tests

Date: 2026-08-06

## Goal

Strengthen regression coverage so mixed-case and whitespace-padded
`critical`/`high` severities cannot slip through `_reconcile_approval`, and
confirm non-blocking severities still take the auto-approve path.

## Context

The normalization helper and baseline mixed-case tests already landed with the
blocking-gate fix. This work is test-only expansion to fully cover the
variants called out for the follow-on regression issue (`High`/`HIGH`/
`critical` and peers).

## Decisions

| Topic | Choice |
|---|---|
| Approach | Expand existing parametrize lists (Approach 1) |
| Production code | Unchanged |
| Blocking cases | `High`, `HIGH`, ` high `, `Critical`, `CRITICAL`, ` critical ` |
| Non-blocking cases | `Medium`, `LOW`, `Info` (reject → still auto-approve) |
| Files | `test_code_review_coordinator.py` only |

## Scope

### In scope

- Expand `test_reconcile_approval_treats_mixed_case_critical_high_as_blocking`
  parametrize.
- Expand `test_reconcile_approval_mixed_case_medium_still_auto_approves` into a
  parametrized non-blocking test (rename if needed for clarity).
- Confirm related coordinator tests still pass.

### Out of scope

- Implementing or changing `_normalized_severity` / coordinator gate logic.
- Schema / UI / taxonomy changes.

## Testing

Run the expanded reconcile tests plus existing cap/reconcile neighbors in
`test_code_review_coordinator.py`.

## Risks

None material — pure test expansion of an already-green gate.
