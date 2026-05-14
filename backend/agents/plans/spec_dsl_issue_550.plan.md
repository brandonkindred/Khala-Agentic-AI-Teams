---
name: spec_dsl + spec_dsl_adapter (issue #550)
overview: |
  Step 1 of 8 from #537. Pure-addition of two new modules
  (`spec_dsl.py`, `spec_dsl_adapter.py`) plus their unit tests under
  `backend/agents/investment_team/`. Nothing else in the codebase imports
  them yet; the wiring to `StrategySpec` is step 2 (separate issue).
issue: 550
parent_issue: 537
isProject: false
---

# Plan: `spec_dsl.py` + `spec_dsl_adapter.py` (issue #550)

## Why this is safe to land in isolation

The two new modules are not imported from anywhere else in the codebase yet.
`backend/agents/investment_team/models.py:194-210` still defines
`StrategySpec` with `entry_rules: List[str]`, `exit_rules: List[str]`,
`sizing_rules: List[str]`. Switching those fields to the new DSL is
explicitly out of scope for this issue (step 2 of #537).

Every consumer that today reads the prose lists — see
`StrategySpec`-readers in `strategy_lab/agents/refinement.py`,
`strategy_lab/orchestrator.py`, `strategy_lab/agents/alignment.py`,
`strategy_lab/agents/analysis.py`,
`strategy_lab/quality_gates/strategy_validator.py`, etc. — keeps reading
strings. They are untouched.

## House pattern we are copying

`backend/agents/investment_team/strategy_lab/factors/models.py:39-357` is
the authoritative house pattern for discriminated-union Pydantic DSLs in
this team. We mirror it:

- `_NodeBase(BaseModel)` with `model_config = ConfigDict(extra="forbid")`.
- Every concrete node carries a `Literal[...]` discriminator
  (`type` for factor models, `kind` here per #537's schema).
- Top-level unions written as
  `Annotated[Union[...], Field(discriminator="kind")]`.
- Trailing `Model.model_rebuild()` for each union member so forward refs
  resolve. (We have no self-recursive nodes in this step, but the
  call-site is still required for forward-string annotations used by
  `EntryRuleUnion` / `ExitRule` / `SizingRule`.)

## Files to add

### 1. `backend/agents/investment_team/strategy_lab/spec_dsl.py`

Module structure, top-to-bottom:

1. Header docstring referencing issue #537/#550.
2. `from __future__ import annotations`; typing/Pydantic imports.
3. `ComparisonOp = Literal["gt","lt","ge","le","eq","cross_above","cross_below"]`.
4. `Source = Literal["close","high","low","open","volume","hl2","ohlc4"]`
   (default `"close"`). Restricted set per #537.
5. `_SpecNode(BaseModel)` base — `extra="forbid"`.
6. **IndicatorRef union** (discriminator `"kind"`). Two distinct
   sources of truth, since they live in different files:

   - **Defaults** mirror
     `strategy_lab/executor/indicators.py` exactly — RSI `period=14`,
     MACD `fast=12 / slow=26 / signal=9`, Bollinger `period=20 /
     num_std=2.0`, Stochastic `k_period=14 / d_period=3`, ATR/ADX
     `period=14`.
   - **Bound style** (`ge=2, le=400` etc.) mirrors the existing house
     factor DSL at `strategy_lab/factors/models.py:53-101`. The
     runtime helpers themselves accept any positive integer (the
     coverage-probe registry at
     `strategy_lab/executor/indicators.py:217-266` only enforces
     positive-int / positive-float-for-`num_std`); we apply
     sanity caps in the DSL the same way `factors/models.py` does,
     so a spec authored at e.g. `period=10000` is rejected before it
     reaches the runtime.

   | DSL node       | kind            | params (defaults from `executor/indicators.py`; bound style from `factors/models.py`) |
   |----------------|-----------------|------------------------------------------------------------------|
   | `PriceRef`     | `"price"`       | `field: Source = "close"`                                        |
   | `ConstRef`     | `"const"`       | `value: float`                                                   |
   | `SMARef`       | `"sma"`         | `period: int = Field(ge=2, le=400)`, `source: Source = "close"`  |
   | `EMARef`       | `"ema"`         | `period: int = Field(ge=2, le=400)`, `source: Source = "close"`  |
   | `RSIRef`       | `"rsi"`         | `period: int = Field(default=14, ge=2, le=200)`, `source`        |
   | `MACDRef`      | `"macd"`        | `fast=12 ge=2 le=200`, `slow=26 ge=3 le=400`, `signal=9 ge=2 le=100`, `output: Literal["macd","signal","histogram"] = "macd"`, `source` |
   | `BollingerRef` | `"bollinger"`   | `period=20 ge=5 le=200`, `num_std=2.0 gt=0`, `band: Literal["upper","middle","lower"] = "middle"`, `source` |
   | `ATRRef`       | `"atr"`         | `period=14 ge=2 le=200`                                          |
   | `ADXRef`       | `"adx"`         | `period=14 ge=2 le=200`                                          |
   | `StochasticRef`| `"stochastic"`  | `k_period=14 ge=2 le=200`, `d_period=3 ge=1 le=100`, `output: Literal["k","d"] = "k"` |
   | `VWAPRef`      | `"vwap"`        | (no params; cumulative VWAP per OHLCV bars)                       |

   Note on `BollingerRef.num_std`: the `executor/indicators.py` registry
   marks `num_std` as a `float_kwargs` member, so it must be a positive
   float (`gt=0`); we mirror that here verbatim.

   Why bounds at all when the runtime doesn't enforce them: the same
   reason `factors/models.py` does — to reject obviously-broken spec
   payloads (`period=0`, `period=10000`) at validation time rather
   than letting pandas produce all-NaN columns silently. The chosen
   caps are generous (≥200 for most, ≥400 for SMA/EMA) and match the
   factor DSL exactly so the two stay in sync.

   Tuple-returning indicators (`MACDRef`, `BollingerRef`, `StochasticRef`)
   pick the scalar to expose via the literal `output` / `band` field as
   the issue specifies (this matches the runtime helpers in
   `executor/indicators.py:45-145` returning `(macd, signal, histogram)`,
   `(upper, middle, lower)`, `(%K, %D)`).

   ```python
   IndicatorRef = Annotated[
       Union[PriceRef, ConstRef, SMARef, EMARef, RSIRef, MACDRef,
             BollingerRef, ATRRef, ADXRef, StochasticRef, VWAPRef],
       Field(discriminator="kind"),
   ]
   ```

7. **Predicate**

   ```python
   class Predicate(_SpecNode):
       lhs: IndicatorRef
       op: ComparisonOp
       rhs: IndicatorRef
   ```

   No `kind` discriminator on `Predicate` itself — it is not part of any
   union. (Allowing `ConstRef` on either side covers numeric literals
   such as `"RSI < 30"`; the adapter wraps bare numbers in `ConstRef`.)

8. **EntryRule + UnparsableRule**

   ```python
   class EntryRule(_SpecNode):
       kind: Literal["entry"] = "entry"
       side: Literal["long","short"]
       when: Predicate
       note: str = ""

   class UnparsableRule(_SpecNode):
       kind: Literal["unparsable"] = "unparsable"
       prose: str
       reason: str = ""

   EntryRuleUnion = Annotated[
       Union[EntryRule, UnparsableRule],
       Field(discriminator="kind"),
   ]
   ```

   `UnparsableRule` is shared with the exit slot per the issue (same
   discriminator key `"unparsable"`).

9. **ExitRule union**

   ```python
   class TimeStopRule(_SpecNode):
       kind: Literal["time_stop"] = "time_stop"
       n_bars: int = Field(gt=0)
       note: str = ""

   class StopLossRule(_SpecNode):
       kind: Literal["stop_loss"] = "stop_loss"
       pct: float = Field(gt=0, le=1.0)   # 0.03 = 3 %
       basis: Literal["entry_price","trailing_high","trailing_low"] = "entry_price"
       note: str = ""

   class TakeProfitRule(_SpecNode):
       kind: Literal["take_profit"] = "take_profit"
       pct: float = Field(gt=0)
       note: str = ""

   class SignalExitRule(_SpecNode):
       kind: Literal["signal_exit"] = "signal_exit"
       when: Predicate
       note: str = ""

   ExitRule = Annotated[
       Union[TimeStopRule, StopLossRule, TakeProfitRule,
             SignalExitRule, UnparsableRule],
       Field(discriminator="kind"),
   ]
   ```

10. **SizingRule union**

    ```python
    class FixedFractionSizing(_SpecNode):
        kind: Literal["fixed_fraction"] = "fixed_fraction"
        fraction: float = Field(gt=0, le=1.0)
        note: str = ""

    class VolatilityTargetSizing(_SpecNode):
        kind: Literal["volatility_target"] = "volatility_target"
        target_annual_vol: float = Field(gt=0)
        note: str = ""

    class FixedNotionalSizing(_SpecNode):
        kind: Literal["fixed_notional"] = "fixed_notional"
        notional_usd: float = Field(gt=0)
        note: str = ""

    class UnparsableSizing(_SpecNode):
        kind: Literal["unparsable_sizing"] = "unparsable_sizing"
        prose: str
        reason: str = ""

    SizingRule = Annotated[
        Union[FixedFractionSizing, VolatilityTargetSizing,
              FixedNotionalSizing, UnparsableSizing],
        Field(discriminator="kind"),
    ]
    ```

    `UnparsableSizing` uses a distinct kind value
    (`"unparsable_sizing"`) so it cannot accidentally satisfy
    `EntryRuleUnion` / `ExitRule` at validation time.

11. Trailing `model_rebuild()` calls for every union member that uses a
    forward-string annotation. (Strictly only needed for models that
    reference other Pydantic models by name with `from __future__ import
    annotations`; doing it for every union member keeps us aligned with
    `factors/models.py:339-348` so future edits don't bite us.)

12. **Public formatters** — drop-in replacements for today's prose
    strings:

    - `format_predicate(p: Predicate) -> str` — internal helper.
      `"close > sma(20)"`, `"RSI < 30"`, `"sma(20) crosses above sma(50)"`.
      Uses `_format_indicator_ref` that emits canonical short forms:
      `"close"`/`"high"`/...for `PriceRef`, the bare number for
      `ConstRef`, and `"sma(20)"`/`"rsi(14)"`/`"macd_signal(12,26,9)"`
      etc. for the indicators.
    - `format_rule(rule: EntryRule | ExitRule | UnparsableRule) -> str`
      — internal helper that dispatches on `kind`:
        - `"entry"` → `"long when close > sma(20)"` (omit side prefix
          when implicit-long elsewhere; keep `"short when …"` explicit).
        - `"time_stop"` → `"exit after {n} bars"`.
        - `"stop_loss"` → `"stop loss {pct*100:g}%"`.
        - `"take_profit"` → `"take profit {pct*100:g}%"`.
        - `"signal_exit"` → `"exit when {predicate}"`.
        - `"unparsable"` → the original `prose` verbatim.
    - `format_rules_for_prompt(rules, separator=", ") -> str` — joins
      with `separator`; empty list returns `""`.
    - `format_sizing_rule(sizing: SizingRule) -> str`:
        - `"fixed_fraction"` → `"risk {fraction*100:g}% per trade"`.
        - `"volatility_target"` →
          `"vol-target {target_annual_vol*100:g}%"`.
        - `"fixed_notional"` → `"${notional_usd:.0f} per trade"`.
        - `"unparsable_sizing"` → `prose` verbatim.

      The output strings are chosen to round-trip through the adapter's
      regex set in step 2 (i.e. feeding `format_sizing_rule` back into
      `parse_sizing_rule` produces an equivalent structured rule).

### 2. `backend/agents/investment_team/strategy_lab/spec_dsl_adapter.py`

- Pure regex; no LLM; no Pydantic side-effects beyond constructing DSL
  nodes.
- `import re`; relative import of the DSL types from `.spec_dsl`.
- Module-level compiled-regex constants for readability and perf.

Public API:

```python
def parse_entry_rule(prose: str) -> EntryRule | UnparsableRule: ...
def parse_exit_rule(prose: str) -> ExitRule: ...               # may be UnparsableRule
def parse_sizing_rule(prose: str) -> SizingRule: ...           # may be UnparsableSizing
def parse_rule_list(
    prose_list: list[str],
    kind: Literal["entry","exit"],
) -> list: ...
def parse_sizing_list(prose_list: list[str]) -> SizingRule: ...  # internal helper
```

Pattern order (first match wins; case-insensitive; tolerant of
whitespace / `_` vs `-` in indicator names):

1. **Comparison predicates** —
   `(<indicator>|<price-field>|<number>)\s*(>|<|>=|<=|==|crosses?\s+(above|below))\s*(<indicator>|<price-field>|<number>)`.
   `_parse_indicator_token` recognises `close|high|low|open|volume`,
   bare numbers (→ `ConstRef`), and `name(arg1[,arg2,...])` for the
   nine indicators. Default-args fall through to the DSL defaults.
   Result: `Predicate`. For entry-slot use, wrap into
   `EntryRule(side="long", when=…)`; the side is `"short"` only when
   the prose contains `\bshort\b` (case-insensitive). For exit-slot
   use, wrap into `SignalExitRule(when=…)` (with leading `"exit when"`
   stripped from the predicate text before re-parsing).
2. `^\s*exit\s+when\s+(.+)$` → recurse into the predicate parser.
   Result: `SignalExitRule`.
3. `^\s*exit\s+after\s+(\d+)\s*(bars?|days?|periods?)\s*$` →
   `TimeStopRule(n_bars=int(...))`.
4. `\bstop[- ]?loss[: ]\s*(\d+(?:\.\d+)?)\s*%` or
   `\bstop[- ]?loss[: ]\s*(0?\.\d+)\b` →
   `StopLossRule(pct=...)`. Percent form divides by 100; decimal form
   used verbatim. Apply the `gt=0, le=1.0` bound; values that fail are
   `UnparsableRule(reason="stop_loss out of bounds")`.
5. `\b(take[- ]?profit|target)[: ]\s*(\d+(?:\.\d+)?)\s*%` →
   `TakeProfitRule(pct=.../100)`. Same `gt=0` bound applies.
6. `\b(risk|allocate)\s+(\d+(?:\.\d+)?)\s*%\s+per\s+trade\b` →
   `FixedFractionSizing(fraction=.../100)`.
7. `\bvol(?:atility)?-?target\s+(\d+(?:\.\d+)?)\s*%` →
   `VolatilityTargetSizing(target_annual_vol=.../100)`.
8. `\$(\d+(?:\.\d+)?)\s+(per\s+trade|notional)` →
   `FixedNotionalSizing(notional_usd=...)`.
9. Anything else →
   `UnparsableRule(prose=prose.strip(), reason="no pattern matched")`
   for entry/exit slots, or `UnparsableSizing(prose=...)` for sizing.

Sizing-list collapse (`parse_sizing_list`):

- Empty list → `UnparsableSizing(prose="", reason="empty")`.
- Single element → return `parse_sizing_rule(elem)`.
- Multiple elements → first parsable wins; the remaining entries are
  concatenated into the chosen variant's `note` field (`"; ".join`).
- All-unparsable → return
  `UnparsableSizing(prose="; ".join(prose_list), reason="no pattern matched")`.

### 3. `backend/agents/investment_team/tests/test_spec_dsl.py`

- One round-trip test per union member:
  `dumped = m.model_dump_json(); reread = type(m).model_validate_json(dumped)`
  with `assert reread == m`.
- Discriminator-dispatch tests: `IndicatorRef`-style adapter built via
  `pydantic.TypeAdapter(IndicatorRef)`; feed `{"kind":"rsi"}` → assert
  `RSIRef`. Repeat for `EntryRuleUnion`, `ExitRule`, `SizingRule`.
- Bounds-violation tests (must raise `ValidationError`):
  - `SMARef(period=1)`, `SMARef(period=401)`.
  - `BollingerRef(period=20, num_std=-1)`.
  - `StopLossRule(pct=-0.01)`, `StopLossRule(pct=1.5)`.
  - `Predicate(lhs=..., op="bogus", rhs=...)`.
  - Missing required: `MACDRef()` with no `fast` etc. (these default,
    so the bounds-test is `MACDRef(fast=1)` to trip `ge=2`).
- Forbid-extra:
  `RSIRef.model_validate({"kind":"rsi","period":14,"foo":1})` raises.
- `format_rules_for_prompt` / `format_sizing_rule` golden strings:
  - `format_rule(EntryRule(side="long", when=Predicate(lhs=PriceRef(field="close"), op="gt", rhs=SMARef(period=20)))) == "long when close > sma(20)"`.
  - `format_rule(EntryRule(side="long", when=Predicate(lhs=RSIRef(period=14), op="lt", rhs=ConstRef(value=30)))) == "long when rsi(14) < 30"`.
  - `format_rule(TimeStopRule(n_bars=5)) == "exit after 5 bars"`.
  - `format_rule(StopLossRule(pct=0.03)) == "stop loss 3%"`.
  - `format_rule(TakeProfitRule(pct=0.05)) == "take profit 5%"`.
  - `format_rule(SignalExitRule(when=Predicate(lhs=RSIRef(period=14), op="gt", rhs=ConstRef(value=70)))) == "exit when rsi(14) > 70"`.
  - `format_sizing_rule(FixedFractionSizing(fraction=0.02)) == "risk 2% per trade"`.
  - `format_sizing_rule(VolatilityTargetSizing(target_annual_vol=0.10)) == "vol-target 10%"`.
  - `format_sizing_rule(FixedNotionalSizing(notional_usd=50000)) == "$50000 per trade"`.

### 4. `backend/agents/investment_team/tests/test_spec_dsl_adapter.py`

- Full pattern matrix (one parametrised test per row):
  - `"close > sma(20)"` → `EntryRule(side="long", when=Predicate(PriceRef("close"), "gt", SMARef(period=20)))`.
  - `"RSI < 30"` → `EntryRule(side="long", when=Predicate(RSIRef(period=14), "lt", ConstRef(value=30)))`.
  - `"short when rsi(70) > 70"` → `EntryRule(side="short", ...)`.
  - `"sma(20) crosses above sma(50)"` → `EntryRule(..., op="cross_above", ...)`.
  - `"exit when RSI > 70"` → `SignalExitRule(when=Predicate(RSIRef(period=14), "gt", ConstRef(value=70)))`.
  - `"exit after 5 bars"` / `"exit after 10 days"` → `TimeStopRule(n_bars=5)` / `TimeStopRule(n_bars=10)`.
  - `"stop loss 3%"` / `"stop-loss: 0.03"` → `StopLossRule(pct=0.03)`.
  - `"take profit 5%"` / `"target 5%"` → `TakeProfitRule(pct=0.05)`.
  - `"risk 2% per trade"` / `"allocate 2% per trade"` → `FixedFractionSizing(fraction=0.02)`.
  - `"vol-target 10%"` / `"volatility-target 10%"` → `VolatilityTargetSizing(target_annual_vol=0.10)`.
  - `"$50000 per trade"` / `"$50000 notional"` → `FixedNotionalSizing(notional_usd=50000)`.
- Unparsable prose:
  - `parse_entry_rule("enter on bullish momentum")` →
    `UnparsableRule(prose="enter on bullish momentum", reason="no pattern matched")`.
  - `parse_exit_rule("vibes")` → `UnparsableRule(...)`.
  - `parse_sizing_rule("size up if confident")` → `UnparsableSizing(prose="size up if confident", ...)`.
- Sizing-list collapse:
  - `parse_sizing_list([])` → `UnparsableSizing(prose="", reason="empty")`.
  - `parse_sizing_list(["risk 2% per trade"])` → `FixedFractionSizing(fraction=0.02)`.
  - `parse_sizing_list(["risk 2% per trade", "max 5% gross"])` →
    `FixedFractionSizing(fraction=0.02, note="max 5% gross")`.
  - `parse_sizing_list(["foo", "bar"])` →
    `UnparsableSizing(prose="foo; bar", reason="no pattern matched")`.
- Round-trip via formatters: for each successful parse case, feed the
  result through `format_rule` / `format_sizing_rule` and assert
  re-parsing it returns an equal structured rule. (This is a cheap
  smoke-test that the formatter strings stay regex-compatible.)

## Execution order

1. Write `spec_dsl.py` (DSL types + formatters).
2. Write `test_spec_dsl.py` and iterate against
   `pytest backend/agents/investment_team/tests/test_spec_dsl.py -v`
   until green.
3. Write `spec_dsl_adapter.py` (regex parser).
4. Write `test_spec_dsl_adapter.py` and iterate.
5. Run `ruff check` and `ruff format` on the two new modules; both
   must come back clean before commit.
6. Sanity-grep to confirm no other file imports the new modules
   yet — `git grep -n "spec_dsl"` should match only the four new
   files.
7. Commit + push to `claude/plan-issue-550-Yjl26`; open a draft PR
   that references #550 and notes that wiring to `StrategySpec`
   (step 2 of #537) is intentionally deferred.

## Acceptance gates (from the issue)

- [ ] `spec_dsl.py` defines every union member listed in the issue,
      with `extra="forbid"` everywhere.
- [ ] `spec_dsl_adapter.py` matches the documented prose patterns.
- [ ] `pytest backend/agents/investment_team/tests/test_spec_dsl.py
      backend/agents/investment_team/tests/test_spec_dsl_adapter.py -v`
      passes.
- [ ] `ruff check backend/agents/investment_team/strategy_lab/spec_dsl.py
      backend/agents/investment_team/strategy_lab/spec_dsl_adapter.py`
      is clean.
- [ ] `git grep -n "from .* import .*spec_dsl"` shows no consumers
      besides the new test files.

## Out of scope (deferred)

- Changing `StrategySpec.entry_rules` / `exit_rules` / `sizing_rules`
  from `list[str]` to the DSL types — that's step 2 of #537.
- Updating prompts, validators, alignment, analysis, orchestrator,
  refinement, zero-trade-repair, or quality-gate code paths to consume
  the DSL.
- Compiler (#C), SpecReadinessGate (#D2), CodeConformanceGate (#E1),
  deterministic alignment (#G).
