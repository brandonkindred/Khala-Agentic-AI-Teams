"""Phase 6 performance summary — read perf samples and emit p50/p95 latencies.

Consumes the JSONL file written by ``test_e2e_smoke.py`` (one line per invoke,
fields: ``agent_id``, ``team``, ``phase`` ∈ {cold,warm}, ``total_ms``) and prints
a per-phase summary plus a markdown row that can be pasted into the sandbox
README's Capacity section.

Run::

    python backend/agents/agent_provisioning_team/tests/scripts/phase6_perf_summary.py

Override the input file with ``--log <path>`` (default
``$AGENT_CACHE/agent_provisioning/phase6_perf.jsonl``).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path


def _default_log_path() -> Path:
    # Mirrors agent_provisioning_team.sandbox.state.resolve_cache_path; kept
    # inline so this script can run without bootstrapping the agents/ tree.
    return Path(
        os.environ.get("AGENT_CACHE", "/tmp/agents"), "agent_provisioning", "phase6_perf.jsonl"
    )


def _load(path: Path) -> list[dict]:
    if not path.exists():
        print(f"perf log not found: {path}", file=sys.stderr)
        sys.exit(2)
    rows: list[dict] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rows.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            print(f"warning: skipping malformed line {line_no}: {exc}", file=sys.stderr)
    return rows


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile — matches numpy's `linear` method."""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    sorted_v = sorted(values)
    k = (len(sorted_v) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_v) - 1)
    frac = k - lo
    return sorted_v[lo] * (1 - frac) + sorted_v[hi] * frac


def _summary(rows: list[dict], phase: str) -> dict:
    samples = [r["total_ms"] for r in rows if r.get("phase") == phase and "total_ms" in r]
    if not samples:
        return {"phase": phase, "n": 0}
    return {
        "phase": phase,
        "n": len(samples),
        "min": int(min(samples)),
        "p50": int(_percentile(samples, 0.50)),
        "p95": int(_percentile(samples, 0.95)),
        "max": int(max(samples)),
        "mean": int(statistics.fmean(samples)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", type=Path, default=_default_log_path())
    args = ap.parse_args()

    rows = _load(args.log)
    cold = _summary(rows, "cold")
    warm = _summary(rows, "warm")

    print(f"perf log: {args.log}")
    print(f"samples:  {len(rows)}")
    print()

    for s in (cold, warm):
        if s["n"] == 0:
            print(f"  {s['phase']:5s} — no samples")
            continue
        print(
            f"  {s['phase']:5s} n={s['n']:3d}  min={s['min']:5d}ms  p50={s['p50']:5d}ms  "
            f"p95={s['p95']:5d}ms  max={s['max']:5d}ms  mean={s['mean']:5d}ms"
        )

    print()
    print("# Capacity-table snippet for sandbox/README.md:")
    print()
    print("| Phase | n | p50 (ms) | p95 (ms) |")
    print("|---|---|---|---|")
    if cold["n"]:
        print(f"| cold-start | {cold['n']} | {cold['p50']} | {cold['p95']} |")
    if warm["n"]:
        print(f"| warm-invoke | {warm['n']} | {warm['p50']} | {warm['p95']} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
