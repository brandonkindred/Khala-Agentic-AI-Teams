"""Parser-coverage audit CLI.

Reads ingredient lines (one per line or one per CSV row) and reports:

- Overall resolution rate: ``canonical_id is not None``.
- Low-confidence rate: ``confidence < 0.85``.
- Top-N unresolved strings (prioritization signal for catalog
  curation — SPEC-005 §4.9).
- Top reason-tag frequencies.

Invoked as:

    python -m nutrition_meal_planning_team.ingredient_kb.cli.audit PATH

The output is plain-text stats suitable for piping into PR
descriptions during KB expansion work.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path
from typing import Iterable

from ..parser import parse_ingredient


def _iter_lines(path: Path) -> Iterable[str]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            for row in reader:
                if row:
                    yield row[0]
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield line


def run(path: Path, top_n: int = 20) -> dict:
    total = 0
    resolved = 0
    low_conf = 0
    unresolved_counter: Counter[str] = Counter()
    reason_counter: Counter[str] = Counter()
    for line in _iter_lines(path):
        total += 1
        parsed = parse_ingredient(line)
        if parsed.canonical_id is not None and parsed.confidence >= 0.85:
            resolved += 1
        elif parsed.canonical_id is not None:
            low_conf += 1
        else:
            unresolved_counter[line] += 1
        for reason in parsed.reasons:
            reason_counter[reason] += 1

    return {
        "total": total,
        "resolved": resolved,
        "low_confidence": low_conf,
        "unresolved": total - resolved - low_conf,
        "coverage_pct": (resolved / total * 100.0) if total else 0.0,
        "top_unresolved": unresolved_counter.most_common(top_n),
        "top_reasons": reason_counter.most_common(top_n),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="ingredient_kb parser coverage audit",
    )
    ap.add_argument("path", type=Path, help="Text or CSV file of ingredient lines")
    ap.add_argument("--top", type=int, default=20, help="Top-N unresolved / reasons")
    args = ap.parse_args()
    result = run(args.path, top_n=args.top)

    print(f"total:          {result['total']}")
    print(f"resolved:       {result['resolved']}")
    print(f"low_confidence: {result['low_confidence']}")
    print(f"unresolved:     {result['unresolved']}")
    print(f"coverage_pct:   {result['coverage_pct']:.2f}%")
    print()
    print(f"Top-{args.top} unresolved (curation backlog):")
    for line, count in result["top_unresolved"]:
        print(f"  [{count:>4}]  {line}")
    print()
    print(f"Top-{args.top} reasons:")
    for reason, count in result["top_reasons"]:
        print(f"  [{count:>4}]  {reason}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
