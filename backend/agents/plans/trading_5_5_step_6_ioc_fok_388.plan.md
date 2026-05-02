---
name: Trading 5/5 — Step 6 — IOC / FOK time-in-force pre-check in fill_simulator
issue: 388
parent_issue: 379
overview: |
  Lift the submission-time runtime gate on tif=IOC|FOK and teach the
  fill simulator to honour both. FOK rejects on this bar if the bar
  can't fully absorb the order (participation-cap clip, or exit-side
  pos.qty shortfall); IOC lets whatever the bar can absorb through and
  force-cancels the remainder regardless of unfilled_policy. LIMIT
  IOC/FOK that never trigger on the bar are also cancelled on the same
  bar so they don't silently behave like DAY/GTC. DAY/GTC paths and
  golden parity remain byte-identical.
todos:
  - id: contract-1
    content: Drop the IOC/FOK UnsupportedOrderFeatureError gate in OrderRequest.validate_prices
    status: pending
  - id: sim-1
    content: Add FOK pre-check before money math in FillSimulator.process_bar (entry- and exit-side)
    status: pending
  - id: sim-2
    content: Add IOC override in _handle_entry_remainder and _handle_exit_remainder
    status: pending
  - id: sim-3
    content: Cancel-this-bar branch for untriggered IOC/FOK (terms is None) with same-side add-on suppression
    status: pending
  - id: tests-1
    content: New test_tif.py covering the four acceptance bullets + same-side add-on regression
    status: pending
  - id: tests-2
    content: Update test_contract_gates.py / test_order_book.py to drop the now-removed gate assertions
    status: pending
  - id: parity-1
    content: Confirm DAY/GTC pathways unchanged; run golden simulator-invariants suite
    status: pending
isProject: false
---

# Spec

## Problem

`TimeInForce` (DAY/GTC/IOC/FOK) was introduced as a schema field in
Step 1 of #379 (#383), but `OrderRequest.validate_prices` rejects
`IOC` and `FOK` with an `UnsupportedOrderFeatureError("#388")`. Until
this step lifts that gate, strategies that ask for IOC or FOK fail at
submission time even though every other prerequisite has shipped:

- partial-fill emission and `unfilled_policy=REQUEUE_NEXT_BAR`
  semantics — Step 4 (#386).
- `unfilled_policy=TWAP_N` slicing across N bars — Step 5 (#387).
- `PendingOrder` `cumulative_filled_qty` / `remaining_qty` /
  `working_against_entry_order_id` plumbing — Step 1/4.
- `RealisticExecutionModel` participation cap producing a partial
  `FillTerms.qty_fraction` — issue #248.

The runtime support for IOC/FOK is the only missing ingredient.

## Goals

1. IOC: emit a `Fill(fill_kind=PARTIAL, unfilled_qty>0)` for whatever
   the bar absorbs, then force-cancel the remainder regardless of
   `req.unfilled_policy`. The strategy still sees the unfilled qty on
   the partial Fill.
2. FOK: emit a single `Fill(fill_kind=REJECTED, qty=0,
   unfilled_qty=requested_qty)` with no `Position` mutation, no money
   math, no requeue when the bar can't fully absorb the order.
3. Untriggered IOC/FOK on the same bar (e.g. a LIMIT IOC whose limit
   price didn't cross): cancel-this-bar with a `REJECTED` Fill so the
   order doesn't silently degrade to DAY/GTC.
4. DAY and GTC behaviour unchanged.
5. Golden parity preserved: `tests/golden/test_simulator_invariants.py`
   stays byte-identical.

## Non-goals (this step)

- Brackets / OCO siblings cancellation — that's Step 7 (#389).
- Trailing stops — Step 8 (#390).
- DAY-at-session-close cancellation semantics — DAY currently behaves
  as GTC in the bar engine; this step does not change that and #379's
  parent acceptance bullet for DAY-at-close is owned by a later step.

# Design

## FOK semantics — "all of nothing on this bar"

A FOK order must fill its entire requested qty on the bar it triggers
or be rejected. Two ways the bar can fail it:

1. **Entry-side / partial-trigger**: the execution model returns
   `FillTerms.qty_fraction < 1.0` because the participation cap clips
   the order against bar dollar-volume. The order would otherwise
   produce a `PARTIAL` entry fill and (depending on policy) requeue or
   drop.
2. **Exit-side / position shortfall**: the strategy asked to exit more
   shares than the position currently holds (`po.remaining_qty >
   pos.qty`). `_fill_exit` would otherwise clip via
   `min(target_qty, pos.qty)` and emit a `PARTIAL` even though the
   execution model returned `qty_fraction == 1.0`.

The FOK pre-check runs **before** any money math (`Position.open`,
`Portfolio.partial_close`, risk gate, capital check). On rejection the
simulator emits a synthetic `Fill`:

```
Fill(
    qty=0.0,
    price=round(terms.reference_price, dp),
    fill_kind=REJECTED,
    unfilled_qty=req.qty,
    cumulative_filled_qty=po.cumulative_filled_qty,
    reason="rejected_fok_partial",
)
```

…routes it to `entry_fills` or `exit_fills` based on the existing
`is_entry_side` dispatch flag, then `order_book.remove(po.order_id)`.
No `Position` mutation, no `Portfolio.record_pnl`, no requeue.

Same-side add-on against an open position (`req.side == pos.side` and
not a partial-entry continuation) is excluded from the FOK pre-check;
that path falls through to the existing silent-suppression branch
later in the loop. FOK doesn't change that — the order never had a
chance to fill, so a synthetic REJECTED Fill would mis-signal a real
liquidity event.

## IOC semantics — "fill what you can, cancel the rest"

IOC differs from FOK in that the partial Fill **does** flow through
the normal money-math pipeline:

- `_fill_entry` / `_continue_entry` open or extend the position with
  the partial qty, slippage applies, the `Fill` carries
  `fill_kind=PARTIAL` and `unfilled_qty>0` (or `FILL` and `unfilled=0`
  if the bar fully absorbs).
- Then the per-handler `unfilled_policy` branch fires. IOC must
  override here: regardless of `req.unfilled_policy` (`DROP`,
  `REQUEUE_NEXT_BAR`, `TWAP_N`), the remainder is removed.

We add the override at the top of both `_handle_entry_remainder` and
`_handle_exit_remainder`:

```python
if po.request.tif == TimeInForce.IOC and unfilled > 0:
    self.order_book.remove(po.order_id, was_filled=...)
    return
```

`was_filled` follows the existing rules — `True` for partial entries
(a position has been opened, parent stays bracket-eligible), `False`
for exits (exits never become bracket parents).

## Untriggered IOC/FOK — "still cancel this bar"

When the execution model returns `terms is None` (e.g. a LIMIT IOC
whose limit price didn't cross the bar's range), the existing code
falls through to the `continue` at the end of the snapshot loop and
the order survives to the next bar — making IOC/FOK silently behave
like DAY/GTC, which is exactly the bug this step is supposed to fix.

We add a branch: if `req.tif in (IOC, FOK)` and `terms is None`,
emit a `REJECTED` Fill with `reason="rejected_{tif}_no_trigger"` and
`order_book.remove`. Reference price is unavailable here, so we
report `bar.close` as a cosmetic price field; no money math.

**Symmetry with the same-side add-on path**: the *triggered* path's
same-side branch (`req.side == pos.side` while pos is open) silently
removes without emitting a Fill. The untriggered path must behave the
same way for same-side add-ons — otherwise an open LONG with an
add-on LONG IOC LIMIT that doesn't cross would emit a synthetic
`REJECTED` exit Fill (because `is_entry_side == False` when a position
exists), which is misleading. We detect the case and fall through
silently:

```python
is_same_side_addon = (
    existing_pos is not None
    and not is_partial_entry_continuation
    and req.side == existing_pos.side
)
if req.tif in (IOC, FOK):
    if is_same_side_addon:
        self.order_book.remove(po.order_id)
        continue
    # …emit REJECTED Fill…
```

# Implementation Plan

1. **Lift the gate (`contract.py`).**
   In `OrderRequest.validate_prices`, drop the four-line block that
   raises `UnsupportedOrderFeatureError` for `tif in (IOC, FOK)`.
   Keep the shape-consistency check
   (`IOC/FOK only valid with market or limit orders`) — that one
   stays live.

2. **Lift the gate's auxiliary tests.**
   - `test_contract_gates.py`: delete `test_ioc_is_gated_until_step_6`
     and `test_fok_is_gated_until_step_6`. Re-target the
     `test_gates_raise_unsupported_order_feature_subclass` fixture from
     `tif=IOC` to `order_type=TRAILING_STOP, stop_price=10.0` so the
     subclass-routing assertion still has a live gate to fire on (Step
     8 still gates trailing stops). Extend
     `test_default_market_order_still_validates` with positive cases
     for `tif=IOC`, `tif=FOK`, and the LIMIT variants.
   - `test_order_book.py`: delete
     `test_submit_attached_rejects_ioc_child`. Update the
     module-level comment to reflect that only `TRAILING_STOP` remains
     gated for attached children.

3. **FOK pre-check in `FillSimulator.process_bar`.**
   After `terms` is resolved (non-`None` branch) and the bar-safety
   assertion fires, before the dispatch into
   `_continue_entry`/`_fill_entry`/`_fill_exit`:

   ```python
   if req.tif == TimeInForce.FOK and not is_same_side_addon:
       fok_partial = terms.qty_fraction < 1.0
       if (
           not is_entry_side
           and existing_pos is not None
           and req.side != existing_pos.side
           and po.remaining_qty > existing_pos.qty
       ):
           fok_partial = True
       if fok_partial:
           # …emit REJECTED Fill, route by is_entry_side, remove…
           continue
   ```

   The double-condition for the exit-side check covers the
   `qty_fraction == 1.0` but `pos.qty < requested` case. Routing flags
   (`is_entry`, `is_partial_entry_continuation`, `is_entry_side`) are
   already computed up-front; reuse them.

4. **Untriggered branch (`terms is None`).**
   Inside the existing `if terms is None:` branch, add the
   IOC/FOK cancel-this-bar logic *before* the TWAP_N elapsed-bar tick
   so a `LIMIT IOC` with `unfilled_policy=TWAP_N` (legal) cancels
   instead of consuming a slice. Apply the same-side add-on
   suppression described above.

5. **IOC override in remainder handlers.**
   First statement of `_handle_entry_remainder` and
   `_handle_exit_remainder`:

   ```python
   if po.request.tif == TimeInForce.IOC and unfilled > 0:
       self.order_book.remove(po.order_id, was_filled=was_filled)  # entry side
       # OR
       self.order_book.remove(po.order_id)                          # exit side
       return
   ```

   Both run *after* the partial Fill has already been emitted by the
   caller — the override only changes requeue-vs-remove, never the
   Fill payload.

6. **Tests — new file `test_tif.py`.**
   See "Test Plan" below.

7. **Run the golden parity suite.**
   `pytest backend/agents/investment_team/tests/golden/` — assert no
   diffs in `simulator_invariants` snapshots. The default TIF stays
   `DAY` and the new code only branches on `IOC`/`FOK`, so this should
   be a no-op, but it's the load-bearing parity gate for #379.

# Files

- `backend/agents/investment_team/trading_service/strategy/contract.py`
  — drop the IOC/FOK runtime gate (~5 lines removed).
- `backend/agents/investment_team/trading_service/engine/fill_simulator.py`
  — FOK pre-check, untriggered branch, IOC remainder override
  (~130 lines added).
- `backend/agents/investment_team/tests/test_tif.py` — new file,
  ~260 lines.
- `backend/agents/investment_team/tests/test_contract_gates.py`
  — delete IOC/FOK gate tests, re-target subclass-routing fixture,
  add positive validation cases.
- `backend/agents/investment_team/tests/test_order_book.py`
  — delete `test_submit_attached_rejects_ioc_child`, update gate
  comment.

# Test Plan

`backend/agents/investment_team/tests/test_tif.py`:

| # | Test | Acceptance |
|---|------|------------|
| 1 | `test_fok_low_adv_emits_rejected_fill_and_no_position` — FOK + low-volume bar (qty=2000 @ price=100, volume=10_000 → ~50% participation clip) | Single `REJECTED` Fill, qty=0, unfilled=2000, no Position, no exit fill, `order_book.all_pending() == []`, follow-up bar produces nothing. |
| 2 | `test_fok_full_absorption_emits_full_fill` — FOK + bar with volume=10_000_000 | Single `FULL` Fill, qty=2000, position opened, order removed. |
| 3 | `test_ioc_low_adv_emits_partial_then_removes_order` — IOC + same low-ADV bar | `PARTIAL` Fill at qty=1000, unfilled=1000, position opened with qty=1000, no pending order, follow-up bar produces nothing. |
| 4 | `test_ioc_overrides_requeue_next_bar_policy` — IOC + `unfilled_policy=REQUEUE_NEXT_BAR` | `PARTIAL` Fill, **no requeue** despite the policy. |
| 5 | `test_no_trigger_ioc_same_side_addon_drops_silently` — same-side LONG IOC LIMIT add-on against an open LONG position with an unreachable limit price | No Fill emitted (entry or exit), add-on order removed silently, original position untouched. (Added in PR review.) |

Fixture helpers (`_bar`, `_make_simulator`, `_entry_order`) pin
participation cap to 0.10 and use `slippage_bps=0`,
`transaction_cost_bps=0` so the math in cases 1–4 is exact:

```
At price=100, volume=10_000  → bar_$_volume=1_000_000, capacity=100_000
                               qty=2_000 → notional=200_000 → 50% clip.
At price=100, volume=10_000_000 → fully absorbs qty=2_000.
```

Existing partial-fill / TWAP_N / order_book / contract-gate suites
must all stay green:

```
pytest backend/agents/investment_team/tests/test_partial_fills.py
pytest backend/agents/investment_team/tests/test_order_book.py
pytest backend/agents/investment_team/tests/test_contract_gates.py
pytest backend/agents/investment_team/tests/golden/
```

# Acceptance Criteria

Mirrors the issue body verbatim, plus the review-driven addition:

- [ ] `test_fok_low_adv_emits_rejected_fill_and_no_position` passes:
      single `Fill(fill_kind=REJECTED)`, no `Position` created, no
      requeue.
- [ ] `test_fok_full_absorption_emits_full_fill` passes:
      `fill_kind=FULL`.
- [ ] `test_ioc_low_adv_emits_partial_then_removes_order` passes:
      `fill_kind=PARTIAL`, `unfilled_qty>0`, order removed, next bar
      sees no pending order.
- [ ] `test_ioc_overrides_requeue_next_bar_policy` passes: IOC
      overrides `REQUEUE_NEXT_BAR`, no requeue.
- [ ] `test_no_trigger_ioc_same_side_addon_drops_silently` passes:
      no synthetic Fill on same-side IOC LIMIT add-ons that don't
      trigger.
- [ ] DAY/GTC behaviour unchanged
      (`test_partial_fills.py`, `test_order_book.py` green).
- [ ] Golden parity preserved
      (`tests/golden/test_simulator_invariants.py` byte-identical).

# Risks & Considerations

- **Routing misclassification**: `is_entry_side` is computed against
  `existing_pos` *before* any mutation. Any FOK reject that fires
  after a partial entry would still route to `entry_fills` because
  `is_partial_entry_continuation` is True. Verified by the existing
  partial-fill + FOK matrix in `test_partial_fills.py`.
- **Same-side add-on asymmetry** (caught in PR review of #420): the
  triggered same-side path silently removes; the untriggered path
  must do the same, otherwise an open LONG with an unreachable LONG
  IOC LIMIT add-on emits a phantom `REJECTED` Fill on
  `outcome.exit_fills`. Test #5 above locks this in.
- **TWAP_N + IOC interaction**: `unfilled_policy=TWAP_N` is a legal
  combo with `tif=IOC` per the existing shape-consistency rules (no
  cross-field gate). The IOC override fires inside both remainder
  handlers *before* the TWAP_N requeue branch, and the untriggered
  branch's IOC cancel runs before the TWAP_N elapsed-bar tick — so a
  TWAP_N IOC always cancels on the first bar regardless of slices
  remaining. That matches the IOC contract.
- **Submission-time shape gate stays live**: even with the runtime
  gate gone, `IOC/FOK only valid with market or limit orders` still
  fires in `validate_prices` — STOP IOC continues to fail at
  submission time, which is the correct behaviour.
- **Default TIF is `DAY`**: no strategy in the repo currently
  submits IOC or FOK, so this step is purely additive surface area
  for future strategies.

# Sequencing

Step 6 of 10 in #379. Independent of Steps 7 (brackets/OCO, #389)
and 8 (trailing stops, #390). Should land before further strategy
authoring so authors can pick IOC/FOK explicitly rather than work
around the runtime gate.
