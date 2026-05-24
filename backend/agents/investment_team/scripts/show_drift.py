"""Print the spec/code revision history for a Strategy Lab record.

Reads a single ``StrategyLabRecord`` from the job service and displays
the spec mutation ledger (``spec_history``), code mutation ledger
(``code_history``), gate timeline summary, and rule-implementation
coverage map.

Run from ``backend/`` (same directory as ``Makefile``)::

    PYTHONPATH=agents python3 -m investment_team.scripts.show_drift <lab_record_id>

Requires ``JOB_SERVICE_URL`` to be set (same env var as the running API).
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Optional


def _print_spec_history(revisions: List[Dict[str, Any]]) -> None:
    if not revisions:
        print("\n  (no spec revisions)")
        return
    for i, rev in enumerate(revisions):
        print(f"\n  [{i}] phase={rev.get('phase')}  agent={rev.get('agent')}")
        print(f"      reason: {rev.get('reason', '')[:120]}")
        print(f"      before: {rev.get('before_hash', '')[:16]}...")
        print(f"      after:  {rev.get('after_hash', '')[:16]}...")
        if rev.get("gate_failures"):
            print(f"      gate_failures: {rev['gate_failures']}")
        diff = rev.get("diff", "")
        if diff:
            for line in diff.splitlines()[:20]:
                print(f"      {line}")
            if len(diff.splitlines()) > 20:
                print(f"      ... ({len(diff.splitlines()) - 20} more lines)")


def _print_code_history(revisions: List[Dict[str, Any]]) -> None:
    if not revisions:
        print("\n  (no code revisions)")
        return
    for i, rev in enumerate(revisions):
        print(f"\n  [{i}] phase={rev.get('phase')}  agent={rev.get('agent')}")
        print(f"      reason: {rev.get('reason', '')[:120]}")
        print(f"      before: {rev.get('before_hash', '')[:16]}...")
        print(f"      after:  {rev.get('after_hash', '')[:16]}...")
        diff = rev.get("diff", "")
        if diff:
            line_count = len(diff.splitlines())
            print(f"      ({line_count} diff lines)")


def _print_gate_timeline(events: List[Dict[str, Any]]) -> None:
    if not events:
        print("\n  (no gate events)")
        return
    passed = sum(1 for e in events if e.get("passed"))
    failed = len(events) - passed
    print(f"\n  {len(events)} gate events: {passed} passed, {failed} failed")
    for e in events:
        if not e.get("passed"):
            mark = "FAIL"
            print(f"    [{mark}] {e.get('gate_name')} ({e.get('severity')}) — {e.get('details', '')[:80]}")


def _print_rule_map(rules: List[Dict[str, Any]]) -> None:
    if not rules:
        print("\n  (no rule implementation map)")
        return
    for r in rules:
        traded = r.get("traded_count", 0)
        refs = r.get("code_line_refs", [])
        marker = " *** ZERO TRADES" if traded == 0 else ""
        ref_str = f"  lines={refs}" if refs else ""
        print(f"    {r.get('rule_id', '?'):30s}  traded={traded}{ref_str}{marker}")


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Show spec/code drift for a Strategy Lab record")
    parser.add_argument("lab_record_id", help="lab-XXXXXXXX record identifier")
    args = parser.parse_args(argv)

    try:
        from job_service_client import JobServiceClient
    except ImportError:
        print("ERROR: run from backend/ with PYTHONPATH=agents", file=sys.stderr)
        sys.exit(1)

    lab_client = JobServiceClient(team="investment_strategy_lab_records")
    target = None
    for job in lab_client.list_jobs() or []:
        payload = job.get("data") or {}
        if payload.get("lab_record_id") == args.lab_record_id:
            target = payload
            break

    if target is None:
        print(f"Record {args.lab_record_id!r} not found.", file=sys.stderr)
        sys.exit(1)

    print(f"=== Drift Report: {args.lab_record_id} ===")
    print(f"    strategy_id: {target.get('strategy', {}).get('strategy_id', '?')}")
    print(f"    is_winning:  {target.get('is_winning')}")
    print(f"    created_at:  {target.get('created_at')}")

    print("\n── Spec Revisions ──")
    _print_spec_history(target.get("spec_history", []))

    print("\n── Code Revisions ──")
    _print_code_history(target.get("code_history", []))

    print("\n── Gate Timeline ──")
    _print_gate_timeline(target.get("gate_timeline", []))

    print("\n── Rule Implementation Map ──")
    _print_rule_map(target.get("rule_implementation_map", []))


if __name__ == "__main__":
    main()
