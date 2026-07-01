"""Post-hoc acceptance-criteria audit for recent Strategy Lab runs.

Reads ``investment_strategy_lab_records`` from the job service and evaluates
10 objective acceptance criteria against each record.  Returns exit-code 0
when the pass rate meets the threshold (``--min-pass-rate``, default 80 %),
or 1 otherwise.

Run from ``backend/`` (same directory as ``Makefile``)::

    PYTHONPATH=agents python3 -m investment_team.scripts.audit_recent_runs \\
        --since=30d --sample=10 --min-pass-rate=0.8

Requires ``JOB_SERVICE_URL`` to be set (same env var as the running API).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("audit_recent_runs")

# ---------------------------------------------------------------------------
# Constants replicated from quality-gate modules so the audit stays
# decoupled (no runtime import of gate classes or the spec DSL).
# ---------------------------------------------------------------------------

_RISK_LIMIT_TIGHTEN_DIR: Dict[str, Optional[str]] = {
    "max_gross_leverage": "lower",
    "max_position_pct": "lower",
    "max_symbol_concentration_pct": "lower",
    "max_open_positions": "lower",
    "target_annual_vol": "lower",
    "vol_lookback_days": None,
}

_RISK_LIMIT_KEYS = frozenset(_RISK_LIMIT_TIGHTEN_DIR.keys())

# Indicator concept vocabulary for narrative mentions, mirrored from
# ``spec_readiness`` as a decoupled literal copy (the audit imports no gate/DSL
# modules at runtime). The regex matches both DSL tokens *and* the common prose
# forms ("on-balance volume", "money flow", "rate of change", "williams"/
# "williams_r", …), and the map resolves each match to the DSL indicator(s) it
# names — so a narrative that name-drops an indicator can't slip past
# ``check_narrative_fidelity`` whether it uses prose or the exact DSL identifier.
# ``tests/test_audit_recent_runs.py`` asserts both stay byte-for-byte in sync with
# ``spec_readiness`` and that the map covers every ``IndicatorName``.
_CONCEPT_TERMS = re.compile(
    r"\b(rsi|macd|moving\s+average|ema|sma|bollinger|atr|stochastic|adx|vwap|"
    r"donchian|keltner|obv|on[\s-]balance\s+volume|mfi|money\s+flow|roc|"
    r"rate\s+of\s+change|cci|williams_r|williams)\b",
    re.IGNORECASE,
)
_CONCEPT_TO_INDICATOR_NAMES: dict[str, frozenset[str]] = {
    "rsi": frozenset({"rsi"}),
    "macd": frozenset({"macd"}),
    "moving average": frozenset({"sma", "ema"}),
    "ema": frozenset({"ema"}),
    "sma": frozenset({"sma"}),
    "bollinger": frozenset({"bollinger"}),
    "atr": frozenset({"atr"}),
    "stochastic": frozenset({"stochastic"}),
    "adx": frozenset({"adx"}),
    "vwap": frozenset({"vwap"}),
    "donchian": frozenset({"donchian"}),
    "keltner": frozenset({"keltner"}),
    "obv": frozenset({"obv"}),
    "on balance volume": frozenset({"obv"}),
    "on-balance volume": frozenset({"obv"}),
    "mfi": frozenset({"mfi"}),
    "money flow": frozenset({"mfi"}),
    "roc": frozenset({"roc"}),
    "rate of change": frozenset({"roc"}),
    "cci": frozenset({"cci"}),
    "williams_r": frozenset({"williams_r"}),
    "williams": frozenset({"williams_r"}),
}

_MULTIPLIER_TOL = 1e-6

_POST_DESIGN_PHASES = frozenset({"synthesis", "verification"})


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        result = float(value)
        if result != result:
            return default
        return result
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    status: str  # "PASS", "FAIL", "SKIP"
    details: str = ""


# ---------------------------------------------------------------------------
# Argument helpers
# ---------------------------------------------------------------------------


def _parse_since(value: str) -> datetime:
    m = re.match(r"^(\d+)d$", value)
    if m:
        days = int(m.group(1))
        return datetime.now(tz=timezone.utc) - timedelta(days=days)
    try:
        d = date.fromisoformat(value)
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(
        f"--since must be a duration like '30d' or an ISO date like '2024-06-01', got {value!r}"
    )


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be an integer, got {value!r}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {parsed}")
    return parsed


def _parse_rate(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be a float, got {value!r}") from exc
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError(f"must be between 0.0 and 1.0, got {parsed}")
    return parsed


# ---------------------------------------------------------------------------
# Record loading
# ---------------------------------------------------------------------------


def _parse_created_at(value: str) -> Optional[datetime]:
    """Parse a created_at timestamp into a timezone-aware datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _load_records(
    client: Any,
    since: datetime,
    sample: Optional[int],
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for job in client.list_jobs() or []:
        jid = job.get("job_id")
        if not jid:
            continue
        payload = job.get("data") or {}
        raw_ts = payload.get("created_at", "")
        created_dt = _parse_created_at(raw_ts)
        if created_dt is None:
            if raw_ts:
                logger.warning("Skipping record %s: unparseable created_at %r", jid, raw_ts)
            continue
        if created_dt >= since:
            payload["_job_id"] = jid
            payload["_created_dt"] = created_dt
            records.append(payload)
    records.sort(key=lambda r: r.get("_created_dt", since), reverse=True)
    if sample is not None:
        records = records[:sample]
    return records


# ---------------------------------------------------------------------------
# Spec-field accessors (dict-path helpers)
# ---------------------------------------------------------------------------


def _spec(record: Dict[str, Any]) -> Dict[str, Any]:
    return record.get("strategy") or {}


def _backtest(record: Dict[str, Any]) -> Dict[str, Any]:
    return record.get("backtest") or {}


def _result(record: Dict[str, Any]) -> Dict[str, Any]:
    return _backtest(record).get("result") or {}


def _config(record: Dict[str, Any]) -> Dict[str, Any]:
    return _backtest(record).get("config") or {}


def _trades(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    return _backtest(record).get("trades") or []


# ---------------------------------------------------------------------------
# The 10 acceptance-criteria checks
# ---------------------------------------------------------------------------


def _parse_diff_kv(line: str) -> tuple[Optional[str], Optional[float]]:
    """Extract (key, numeric_value) from a unified-diff +/- line, or (None, None)."""
    content = line[1:].strip().strip(",").strip('"')
    if not content or content in ("{", "}", "[", "]"):
        return None, None
    if ":" not in content:
        return content.strip('"'), None
    key = content.split(":")[0].strip().strip('"')
    val_str = content.split(":", 1)[1].strip().rstrip(",").strip('"')
    try:
        return key, float(val_str)
    except (ValueError, TypeError):
        return key, None


def check_spec_stability(record: Dict[str, Any]) -> CheckResult:
    """Spec must not mutate after the design phase, except to tighten risk limits."""
    name = "spec_stability"
    history = record.get("spec_history")
    if history is None:
        return CheckResult(name, "SKIP", "spec_history missing (legacy record)")

    post_design = [h for h in history if h.get("phase") in _POST_DESIGN_PHASES]
    if not post_design:
        return CheckResult(name, "PASS")

    for rev in post_design:
        diff_text = rev.get("diff", "")
        removed: Dict[str, float] = {}
        added: Dict[str, float] = {}
        removed_keys: set[str] = set()
        added_keys: set[str] = set()
        for line in diff_text.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("-"):
                key, val = _parse_diff_kv(line)
                if key is None:
                    continue
                if key != "risk_limits" and key not in _RISK_LIMIT_KEYS:
                    return CheckResult(
                        name,
                        "FAIL",
                        f"Post-design spec revision in phase '{rev.get('phase')}' "
                        f"touched non-risk-limits field: {key}",
                    )
                if key in _RISK_LIMIT_KEYS:
                    removed_keys.add(key)
                if val is not None:
                    removed[key] = val
            elif line.startswith("+"):
                key, val = _parse_diff_kv(line)
                if key is None:
                    continue
                if key != "risk_limits" and key not in _RISK_LIMIT_KEYS:
                    return CheckResult(
                        name,
                        "FAIL",
                        f"Post-design spec revision in phase '{rev.get('phase')}' "
                        f"touched non-risk-limits field: {key}",
                    )
                if key in _RISK_LIMIT_KEYS:
                    added_keys.add(key)
                if val is not None:
                    added[key] = val

        for key in added_keys | removed_keys:
            has_old = key in removed
            has_new = key in added
            if has_old and has_new:
                direction = _RISK_LIMIT_TIGHTEN_DIR.get(key)
                if direction is None:
                    return CheckResult(
                        name,
                        "FAIL",
                        f"Post-design spec revision in phase '{rev.get('phase')}' "
                        f"changed immutable risk-limit field: {key}",
                    )
                if direction == "lower" and added[key] > removed[key]:
                    return CheckResult(
                        name,
                        "FAIL",
                        f"Post-design spec revision in phase '{rev.get('phase')}' "
                        f"loosened {key}: {removed[key]} -> {added[key]}",
                    )
            elif has_old != has_new:
                return CheckResult(
                    name,
                    "FAIL",
                    f"Post-design spec revision in phase '{rev.get('phase')}' "
                    f"structurally changed {key} (null/value mismatch)",
                )

    return CheckResult(name, "PASS")


def check_rule_implementation(record: Dict[str, Any]) -> CheckResult:
    """Every spec rule must have a rule_implementation_map entry with code refs."""
    name = "rule_implementation"
    spec = _spec(record)
    if spec.get("requires_custom_code"):
        return CheckResult(name, "SKIP", "requires_custom_code=True")

    entry_rules = spec.get("entry_rules") or []
    exit_rules = spec.get("exit_rules") or []

    rim = record.get("rule_implementation_map")
    if rim is None:
        return CheckResult(name, "SKIP", "rule_implementation_map missing (legacy record)")
    if not rim:
        if entry_rules or exit_rules:
            return CheckResult(name, "FAIL", "rule_implementation_map empty but spec has rules")
        return CheckResult(name, "SKIP", "No rules in spec")

    expected_ids: List[str] = []
    for i, _ in enumerate(entry_rules):
        expected_ids.append(f"entry[{i}]")

    kind_counts: Dict[str, int] = {}
    for er in exit_rules:
        if not isinstance(er, dict):
            continue
        kind = er.get("kind", "exit")
        idx = kind_counts.get(kind, 0)
        kind_counts[kind] = idx + 1
        expected_ids.append(f"exit:{kind}[{idx}]")

    rim_by_id = {r.get("rule_id"): r for r in rim}
    missing = [rid for rid in expected_ids if rid not in rim_by_id]

    if missing:
        return CheckResult(name, "FAIL", f"Rules missing from map: {', '.join(missing)}")
    return CheckResult(name, "PASS")


def check_universe_fidelity(record: Dict[str, Any]) -> CheckResult:
    """Every trade symbol must be in the spec's target_symbols."""
    name = "universe_fidelity"
    target = _spec(record).get("target_symbols") or []
    if not target:
        return CheckResult(name, "SKIP", "target_symbols empty (universe-agnostic)")

    allowed = {s.upper() for s in target}
    trades = _trades(record)
    if not trades:
        return CheckResult(name, "SKIP", "no trades")

    off_spec = sorted(
        {
            str(t.get("symbol") or "?")
            for t in trades
            if str(t.get("symbol") or "").upper() not in allowed
        }
    )
    if off_spec:
        return CheckResult(name, "FAIL", f"Off-spec symbols: {', '.join(off_spec)}")
    return CheckResult(name, "PASS")


def check_exit_rule_alignment(record: Dict[str, Any]) -> CheckResult:
    """Exit-rule conformance gate must not have critical failures."""
    name = "exit_rule_alignment"
    gate_results = record.get("quality_gate_results") or []

    conformance = [g for g in gate_results if g.get("gate_name") == "exit_rule_conformance"]
    if conformance:
        critical_fails = [
            g for g in conformance if g.get("severity") == "critical" and not g.get("passed")
        ]
        if critical_fails:
            details = "; ".join(g.get("details", "?") for g in critical_fails[:3])
            return CheckResult(name, "FAIL", f"Critical exit-rule failures: {details}")
        return CheckResult(name, "PASS")

    findings = _backtest(record).get("alignment_findings") or []
    alignment_checks = [
        f
        for f in findings
        if f.get("check_name") in ("stop_loss", "take_profit", "signal_exit")
        and f.get("severity") == "critical"
        and not f.get("passed")
    ]
    if findings and not alignment_checks:
        return CheckResult(name, "PASS")
    if alignment_checks:
        details = "; ".join(f.get("details", "?") for f in alignment_checks[:3])
        return CheckResult(name, "FAIL", f"Critical alignment failures: {details}")

    return CheckResult(name, "SKIP", "No exit_rule_conformance gate results or alignment findings")


def check_cost_robustness(record: Dict[str, Any]) -> CheckResult:
    """Annualized return must be >= 0 at the 2x cost-stress multiplier."""
    name = "cost_robustness"
    rows = _result(record).get("cost_stress_results")
    if not rows:
        return CheckResult(name, "SKIP", "No cost_stress_results")

    row_2x = None
    for row in rows:
        mult = row.get("multiplier")
        if mult is None:
            continue
        try:
            if abs(float(mult) - 2.0) < _MULTIPLIER_TOL:
                row_2x = row
                break
        except (ValueError, TypeError):
            continue

    if row_2x is None:
        return CheckResult(name, "SKIP", "No 2.0x multiplier row found")

    ann_return = _safe_float(row_2x.get("annualized_return_pct"))
    if ann_return is None:
        return CheckResult(name, "SKIP", "2.0x row missing or non-numeric annualized_return_pct")
    if ann_return >= 0:
        return CheckResult(name, "PASS")
    return CheckResult(name, "FAIL", f"2x cost-stress annualized return = {ann_return:.2f}% (< 0)")


def check_regime_coverage(record: Dict[str, Any]) -> CheckResult:
    """Every regime with observations must have non-negative cumulative return; deflated Sharpe >= 0."""
    name = "regime_coverage"
    result = _result(record)
    regimes = result.get("regime_results")
    if not regimes:
        return CheckResult(name, "SKIP", "No regime_results")

    dsr = _safe_float(result.get("deflated_sharpe"))
    if dsr is None:
        return CheckResult(name, "FAIL", "deflated_sharpe missing or non-numeric")
    if dsr < 0:
        return CheckResult(name, "FAIL", f"Deflated Sharpe = {dsr:.4f} (< 0)")

    losers = []
    for r in regimes:
        n_obs = _safe_float(r.get("n_obs"), 0.0)
        if not n_obs or n_obs <= 0:
            continue
        cumret = _safe_float(r.get("strategy_cumret"))
        if cumret is None:
            losers.append(f"{r.get('regime', '?')} (cumret=missing)")
        elif cumret < 0:
            losers.append(f"{r.get('regime', '?')} (cumret={cumret:.4f})")
    if losers:
        return CheckResult(name, "FAIL", f"Negative-cumret regimes: {'; '.join(losers)}")
    return CheckResult(name, "PASS")


def _collect_spec_indicators(record: Dict[str, Any]) -> set[str]:
    """Collect indicator names referenced in the spec's entry/exit rule predicates."""
    spec = _spec(record)
    indicators: set[str] = set()

    def _extract_from_predicate(pred: Any) -> None:
        if not isinstance(pred, dict):
            return
        lhs = pred.get("lhs")
        if isinstance(lhs, dict) and lhs.get("name"):
            indicators.add(lhs["name"].lower())
        rhs = pred.get("rhs")
        if isinstance(rhs, dict) and rhs.get("name"):
            indicators.add(rhs["name"].lower())

    for rule in spec.get("entry_rules") or []:
        if isinstance(rule, dict):
            _extract_from_predicate(rule.get("when"))
    for rule in spec.get("exit_rules") or []:
        if isinstance(rule, dict):
            _extract_from_predicate(rule.get("when"))

    return indicators


def check_narrative_fidelity(record: Dict[str, Any]) -> CheckResult:
    """The analysis narrative must only reference indicators present in the spec."""
    name = "narrative_fidelity"
    narrative = record.get("analysis_narrative", "")
    if not narrative:
        return CheckResult(name, "SKIP", "No analysis_narrative")

    spec_indicators = _collect_spec_indicators(record)
    # Match DSL tokens and common prose forms, normalising whitespace exactly as
    # spec_readiness does, then resolve each mention to the indicator(s) it could
    # name. A mention is phantom only when *none* of those indicators is in the
    # spec (so "moving average" is satisfied by either SMA or EMA).
    mentioned = {
        re.sub(r"\s+", " ", m.group(0).lower()) for m in _CONCEPT_TERMS.finditer(narrative)
    }
    phantoms = sorted(
        concept
        for concept in mentioned
        if not (_CONCEPT_TO_INDICATOR_NAMES.get(concept, frozenset()) & spec_indicators)
    )

    if phantoms:
        return CheckResult(name, "FAIL", f"Phantom indicators in narrative: {', '.join(phantoms)}")
    return CheckResult(name, "PASS")


def check_liquidity_realism(record: Dict[str, Any]) -> CheckResult:
    """Liquidity-realism gate must not have critical failures."""
    name = "liquidity_realism"
    gate_results = record.get("quality_gate_results") or []
    liquidity = [g for g in gate_results if g.get("gate_name") == "liquidity_realism"]
    if not liquidity:
        return CheckResult(name, "SKIP", "No liquidity_realism gate results")

    critical_fails = [
        g for g in liquidity if g.get("severity") == "critical" and not g.get("passed")
    ]
    if critical_fails:
        details = "; ".join(g.get("details", "?") for g in critical_fails[:3])
        return CheckResult(name, "FAIL", f"Critical liquidity failures: {details}")
    return CheckResult(name, "PASS")


def check_no_dead_code_rules(record: Dict[str, Any]) -> CheckResult:
    """Every rule in rule_implementation_map must have traded_count > 0."""
    name = "no_dead_code_rules"
    spec = _spec(record)
    if spec.get("requires_custom_code"):
        return CheckResult(name, "SKIP", "requires_custom_code=True")

    rim = record.get("rule_implementation_map")
    if rim is None:
        return CheckResult(name, "SKIP", "rule_implementation_map missing (legacy record)")
    if not rim:
        has_rules = (spec.get("entry_rules") or []) or (spec.get("exit_rules") or [])
        if has_rules:
            return CheckResult(name, "FAIL", "rule_implementation_map empty but spec has rules")
        return CheckResult(name, "SKIP", "rule_implementation_map missing (legacy record)")

    dead = [
        r.get("rule_id", "?")
        for r in rim
        if isinstance(r, dict)
        and (_safe_float(r.get("traded_count"), 0.0) or 0) == 0
        and r.get("rule_id") != "sizing"
    ]
    if dead:
        return CheckResult(name, "FAIL", f"Dead-code rules (traded_count=0): {', '.join(dead)}")
    return CheckResult(name, "PASS")


# ---------------------------------------------------------------------------
# Check registry
# ---------------------------------------------------------------------------

ALL_CHECKS: List[Callable[[Dict[str, Any]], CheckResult]] = [
    check_spec_stability,
    check_rule_implementation,
    check_universe_fidelity,
    check_exit_rule_alignment,
    check_cost_robustness,
    check_regime_coverage,
    check_narrative_fidelity,
    check_liquidity_realism,
    check_no_dead_code_rules,
]

_SHORT_NAMES = {
    "spec_stability": "spec_stab",
    "rule_implementation": "rule_impl",
    "universe_fidelity": "universe",
    "exit_rule_alignment": "exit_align",
    "cost_robustness": "cost_rob",
    "regime_coverage": "regime",
    "narrative_fidelity": "narrative",
    "liquidity_realism": "liquidity",
    "no_dead_code_rules": "dead_code",
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _print_loop_telemetry(records: List[dict]) -> None:
    """Aggregate and print the per-cycle generation-funnel telemetry.

    Pre: ``records`` is the list of persisted lab-record dicts.
    Post: prints a stop-reason histogram, average design-review rounds,
    cumulative critique regressions, the code-path share (compiled / custom /
    not-synthesized), and the top failing gates across all records carrying a
    ``loop_telemetry`` block. Records without the block (legacy rows /
    design-loop-bypass paths) are skipped. Read-only — no record is mutated.
    """
    telemetries = [t for t in (r.get("loop_telemetry") or {} for r in records) if t]
    if not telemetries:
        print("\n--- Generation funnel ---\n(no loop_telemetry on sampled records)")
        return

    stop_reasons: Counter[str] = Counter()
    fail_gates: Counter[str] = Counter()
    code_paths: Counter[str] = Counter()
    rounds_total = 0
    rounds_n = 0
    regressed_total = 0
    for t in telemetries:
        stop_reasons[str(t.get("stop_reason", "unknown"))] += 1
        rounds = t.get("design_review_rounds")
        if isinstance(rounds, int):
            rounds_total += rounds
            rounds_n += 1
        ledger = t.get("critique_ledger") or {}
        regressed_total += int(ledger.get("total_regressed", 0) or 0)
        # Prefer the three-state ``code_path``; fall back to the legacy
        # boolean for records persisted before it was added (those never
        # short-circuit before synthesis with a populated telemetry block,
        # so the compiled/custom split is still accurate for them).
        code_path = t.get("code_path")
        if code_path is None:
            code_path = "custom" if t.get("requires_custom_code") else "compiled"
        code_paths[str(code_path)] += 1
        for gate, count in (t.get("gate_fail_counts") or {}).items():
            fail_gates[gate] += int(count or 0)

    print(f"\n--- Generation funnel ({len(telemetries)} record(s) with telemetry) ---")
    avg_rounds = f"{rounds_total / rounds_n:.1f}" if rounds_n else "n/a"
    print(f"Avg design-review rounds: {avg_rounds}")
    print("Stop reasons: " + ", ".join(f"{reason}={n}" for reason, n in stop_reasons.most_common()))
    print(f"Critique regressions (cumulative): {regressed_total}")
    print(
        "Code path: "
        + ", ".join(
            f"{path}={code_paths.get(path, 0)}"
            for path in ("compiled", "custom", "not_synthesized")
        )
    )
    if fail_gates:
        top = ", ".join(f"{gate}={n}" for gate, n in fail_gates.most_common(5))
        print(f"Top failing gates: {top}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        type=_parse_since,
        default=None,
        help="Only audit records created after this date. "
        "Accepts a duration like '30d' or an ISO date like '2024-06-01'.",
    )
    parser.add_argument(
        "--sample",
        type=_positive_int,
        default=None,
        help="Audit only the N most recent records (must be >= 1).",
    )
    parser.add_argument(
        "--min-pass-rate",
        type=_parse_rate,
        default=0.8,
        help="Minimum pass rate (0.0-1.0) for exit-code 0 (default: 0.8).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from job_service_client import JobServiceClient

    lab_client = JobServiceClient(team="investment_strategy_lab_records")

    since = args.since or datetime(2000, 1, 1, tzinfo=timezone.utc)
    records = _load_records(lab_client, since, args.sample)

    if not records:
        print("No records found matching criteria.")
        return 0

    # --- Run checks ---
    all_results: List[List[CheckResult]] = []
    for rec in records:
        row = [check(rec) for check in ALL_CHECKS]
        all_results.append(row)

    # --- Tabular output ---
    header_names = [
        _SHORT_NAMES.get(c.__name__.removeprefix("check_"), c.__name__) for c in ALL_CHECKS
    ]
    col_w = max(len(n) for n in header_names) + 1
    id_w = 24

    print("\n=== Strategy Lab Health Audit ===")
    since_str = args.since.strftime("%Y-%m-%d") if args.since else "all"
    print(f"Records: {len(records)}  Since: {since_str}  Sample: {args.sample or 'all'}\n")

    header = f"{'lab_record_id':<{id_w}}" + "".join(f"{n:<{col_w}}" for n in header_names)
    print(header)
    print("-" * len(header))

    failures: List[str] = []
    total_pass = 0
    total_evaluated = 0

    for rec, row in zip(records, all_results):
        rid = str(rec.get("lab_record_id") or rec.get("_job_id", "?"))[:id_w]
        line = f"{rid:<{id_w}}"
        for cr in row:
            line += f"{cr.status:<{col_w}}"
            if cr.status == "PASS":
                total_pass += 1
                total_evaluated += 1
            elif cr.status == "FAIL":
                total_evaluated += 1
                failures.append(f"{rid} / {cr.name}: {cr.details}")
        print(line)

    # --- Failure details ---
    if failures:
        print(f"\n--- Failures ({len(failures)}) ---")
        for f in failures:
            print(f)

    # --- Generation-funnel telemetry (read-only) ---
    _print_loop_telemetry(records)

    # --- Summary ---
    if total_evaluated == 0:
        print("\nNo checks were evaluated (all SKIP). Result: NO DATA")
        return 1
    pass_rate = total_pass / total_evaluated
    verdict = "OK" if pass_rate >= args.min_pass_rate else "BELOW THRESHOLD"
    print(
        f"\nPass rate: {pass_rate:.1%} ({total_pass}/{total_evaluated} evaluated)  "
        f"Target: {args.min_pass_rate:.0%}  Result: {verdict}"
    )

    return 0 if pass_rate >= args.min_pass_rate else 1


if __name__ == "__main__":
    sys.exit(main())
