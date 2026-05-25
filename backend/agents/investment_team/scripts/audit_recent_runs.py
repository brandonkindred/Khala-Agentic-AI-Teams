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
    "max_drawdown_pct": "lower",
    "max_open_positions": "lower",
    "target_annual_vol": "lower",
    "vol_lookback_days": None,
}

_RISK_LIMIT_KEYS = frozenset(_RISK_LIMIT_TIGHTEN_DIR.keys())

_INDICATOR_NAMES = frozenset(
    {
        "sma",
        "ema",
        "rsi",
        "macd",
        "bollinger",
        "atr",
        "adx",
        "stochastic",
        "vwap",
    }
)

_DEFAULT_EXPECTED_HOLD_DAYS: Dict[str, float] = {
    "1d": 10.0,
    "1h": 0.5,
    "15m": 0.1,
    "5m": 0.04,
    "1m": 0.01,
}

_MULTIPLIER_TOL = 1e-6

_POST_DESIGN_PHASES = frozenset({"synthesis", "verification"})


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
        created_dt = _parse_created_at(payload.get("created_at", ""))
        if created_dt is not None and created_dt >= since:
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
                if val is not None:
                    added[key] = val

        for key in added:
            if key not in removed:
                continue
            direction = _RISK_LIMIT_TIGHTEN_DIR.get(key)
            if direction is None:
                return CheckResult(
                    name,
                    "FAIL",
                    f"Post-design spec revision in phase '{rev.get('phase')}' "
                    f"changed immutable risk-limit field: {key}",
                )
            old_val = removed[key]
            new_val = added[key]
            if direction == "lower" and new_val > old_val:
                return CheckResult(
                    name,
                    "FAIL",
                    f"Post-design spec revision in phase '{rev.get('phase')}' "
                    f"loosened {key}: {old_val} -> {new_val}",
                )

    return CheckResult(name, "PASS")


def check_rule_implementation(record: Dict[str, Any]) -> CheckResult:
    """Every spec rule must have a rule_implementation_map entry with code refs."""
    name = "rule_implementation"
    spec = _spec(record)
    if spec.get("requires_custom_code"):
        return CheckResult(name, "SKIP", "requires_custom_code=True")

    rim = record.get("rule_implementation_map")
    if not rim:
        return CheckResult(name, "SKIP", "rule_implementation_map missing (legacy record)")

    entry_rules = spec.get("entry_rules") or []
    exit_rules = spec.get("exit_rules") or []

    expected_ids: List[str] = []
    for i, _ in enumerate(entry_rules):
        expected_ids.append(f"entry[{i}]")

    kind_counts: Dict[str, int] = {}
    for er in exit_rules:
        kind = er.get("kind", "exit")
        idx = kind_counts.get(kind, 0)
        kind_counts[kind] = idx + 1
        expected_ids.append(f"exit:{kind}[{idx}]")

    rim_by_id = {r.get("rule_id"): r for r in rim}
    missing = []
    for rid in expected_ids:
        entry = rim_by_id.get(rid)
        if not entry or not entry.get("code_line_refs"):
            missing.append(rid)

    if missing:
        return CheckResult(name, "FAIL", f"Rules missing code_line_refs: {', '.join(missing)}")
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
        {t.get("symbol", "?") for t in trades if t.get("symbol", "").upper() not in allowed}
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
        if f.get("check_name") in ("stop_loss", "take_profit")
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

    ann_return = row_2x.get("annualized_return_pct", 0.0)
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

    dsr = result.get("deflated_sharpe")
    if dsr is not None and dsr < 0:
        return CheckResult(name, "FAIL", f"Deflated Sharpe = {dsr:.4f} (< 0)")

    losers = []
    for r in regimes:
        n_obs = r.get("n_obs", 0)
        cumret = r.get("strategy_cumret", 0.0)
        if n_obs > 0 and cumret < 0:
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
        _extract_from_predicate(rule.get("when"))
    for rule in spec.get("exit_rules") or []:
        _extract_from_predicate(rule.get("when"))

    return indicators


def check_narrative_fidelity(record: Dict[str, Any]) -> CheckResult:
    """The analysis narrative must only reference indicators present in the spec."""
    name = "narrative_fidelity"
    narrative = record.get("analysis_narrative", "")
    if not narrative:
        return CheckResult(name, "SKIP", "No analysis_narrative")

    spec_indicators = _collect_spec_indicators(record)
    phantoms = []
    for ind in sorted(_INDICATOR_NAMES):
        if re.search(rf"\b{ind}\b", narrative, re.IGNORECASE) and ind not in spec_indicators:
            phantoms.append(ind)

    if phantoms:
        return CheckResult(name, "FAIL", f"Phantom indicators in narrative: {', '.join(phantoms)}")
    return CheckResult(name, "PASS")


def check_trade_adequacy(record: Dict[str, Any]) -> CheckResult:
    """Trade count must meet or exceed the expected count for the backtest window."""
    name = "trade_adequacy"
    config = _config(record)
    start_date = config.get("start_date")
    end_date = config.get("end_date")
    timeframe = _spec(record).get("timeframe")
    trades = _trades(record)

    if not start_date or not end_date:
        return CheckResult(name, "SKIP", "Missing backtest dates")
    if not trades:
        return CheckResult(name, "SKIP", "No trades")

    try:
        start_str = str(start_date).split("T")[0]
        end_str = str(end_date).split("T")[0]
        window_days = (date.fromisoformat(end_str) - date.fromisoformat(start_str)).days
    except (ValueError, TypeError):
        return CheckResult(name, "SKIP", "Unparseable backtest dates")
    if window_days <= 0:
        return CheckResult(name, "SKIP", "Non-positive backtest window")

    observed_holds = [t.get("hold_days", 0) for t in trades if (t.get("hold_days") or 0) > 0]
    if observed_holds:
        expected_hold = sum(observed_holds) / len(observed_holds)
    else:
        expected_hold = _DEFAULT_EXPECTED_HOLD_DAYS.get(str(timeframe), 0.0)
    if not expected_hold or expected_hold <= 0:
        return CheckResult(name, "SKIP", "Cannot determine expected hold days")

    expected_count = max(1, round(window_days / expected_hold))
    n_trades = len(trades)
    if n_trades >= expected_count:
        return CheckResult(name, "PASS")
    return CheckResult(
        name,
        "FAIL",
        f"n_trades={n_trades} < expected={expected_count} "
        f"(window={window_days}d, hold={expected_hold:.1f}d)",
    )


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
    if _spec(record).get("requires_custom_code"):
        return CheckResult(name, "SKIP", "requires_custom_code=True")

    rim = record.get("rule_implementation_map")
    if not rim:
        return CheckResult(name, "SKIP", "rule_implementation_map missing (legacy record)")

    dead = [r.get("rule_id", "?") for r in rim if (r.get("traded_count") or 0) == 0]
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
    check_trade_adequacy,
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
    "trade_adequacy": "trade_adeq",
    "liquidity_realism": "liquidity",
    "no_dead_code_rules": "dead_code",
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


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

    # --- Summary ---
    pass_rate = total_pass / total_evaluated if total_evaluated else 1.0
    verdict = "OK" if pass_rate >= args.min_pass_rate else "BELOW THRESHOLD"
    print(
        f"\nPass rate: {pass_rate:.1%} ({total_pass}/{total_evaluated} evaluated)  "
        f"Target: {args.min_pass_rate:.0%}  Result: {verdict}"
    )

    return 0 if pass_rate >= args.min_pass_rate else 1


if __name__ == "__main__":
    sys.exit(main())
