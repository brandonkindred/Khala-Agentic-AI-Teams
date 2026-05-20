"""Print Strategy Lab runs whose target vs traded symbols diverge.

Reads ``investment_strategy_lab_records`` from the job service and surfaces
rows where the spec's ``target_symbols`` and the ledger's ``traded_symbols``
differ — the breadcrumb a reviewer follows for "spec asked for QQQ, but
the ledger traded TSLA".

Run from ``backend/`` (same directory as ``Makefile``)::

    PYTHONPATH=agents python3 -m investment_team.scripts.divergent_provenance \\
        [--strict] [--limit 50]

Requires ``JOB_SERVICE_URL`` to be set (same env var as the running API).

By default a row is "divergent" when ``set(target_symbols) != set(traded_symbols)``.
``--strict`` switches to exact-list comparison (ordered, no dedup).

Rows skipped from comparison (counted separately, never reported as divergent):
  * Legacy / short-circuit rows persisted before issue #533 — they carry a
    default-empty ``data_provenance`` block; there is nothing to compare.
  * Universe-agnostic runs — ``target_symbols`` is intentionally empty so
    the backtester picks the asset-class fallback universe. Comparing
    ``[]`` against the ledger would mark every such run as divergent and
    drown out the real symbol-drift cases this script is meant to surface.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, List, Tuple

logger = logging.getLogger("divergent_provenance")


def _diverges(target: List[str], traded: List[str], *, strict: bool) -> bool:
    if strict:
        return list(target) != list(traded)
    return set(target) != set(traded)


def _is_empty_provenance(prov: dict[str, Any]) -> bool:
    """True for legacy / short-circuit rows that never populated provenance."""
    return (
        not prov.get("target_symbols")
        and not prov.get("fetched_symbols")
        and not prov.get("traded_symbols")
        and not prov.get("provider_used")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exact-list compare instead of set compare.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after printing this many divergent rows.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from job_service_client import JobServiceClient

    lab_client = JobServiceClient(team="investment_strategy_lab_records")

    scanned = 0
    skipped_legacy = 0
    skipped_universe_agnostic = 0
    divergent: List[Tuple[str, str, dict[str, Any]]] = []
    for job in lab_client.list_jobs() or []:
        jid = job.get("job_id")
        if not jid:
            continue
        payload = job.get("data") or {}
        backtest = payload.get("backtest") or {}
        provenance = backtest.get("data_provenance") or {}
        scanned += 1

        if _is_empty_provenance(provenance):
            skipped_legacy += 1
            continue

        target = list(provenance.get("target_symbols") or [])
        # Universe-agnostic specs leave ``target_symbols`` empty by design
        # so the backtester uses the asset-class fallback universe. There
        # is no "intent" to compare against the ledger; reporting these
        # would create systematic false positives.
        if not target:
            skipped_universe_agnostic += 1
            continue

        traded = list(provenance.get("traded_symbols") or [])
        if not _diverges(target, traded, strict=args.strict):
            continue

        strategy_id = (
            payload.get("strategy", {}).get("strategy_id") or backtest.get("strategy_id") or "?"
        )
        divergent.append((str(jid), str(strategy_id), provenance))
        if args.limit is not None and len(divergent) >= args.limit:
            break

    header = (
        f"{'lab_record_id':<24} {'strategy_id':<24} "
        f"{'target':<24} {'fetched':<32} {'traded':<24} provider_used"
    )
    print(header)
    print("-" * len(header))
    for lab_id, strategy_id, prov in divergent:
        target = ",".join(prov.get("target_symbols") or []) or "-"
        fetched = ",".join(prov.get("fetched_symbols") or []) or "-"
        traded = ",".join(prov.get("traded_symbols") or []) or "-"
        provider_used = prov.get("provider_used") or {}
        print(
            f"{lab_id:<24} {strategy_id:<24} "
            f"{target:<24} {fetched:<32} {traded:<24} {provider_used}"
        )

    logger.info(
        "scanned=%d divergent=%d skipped_legacy=%d skipped_universe_agnostic=%d mode=%s",
        scanned,
        len(divergent),
        skipped_legacy,
        skipped_universe_agnostic,
        "strict" if args.strict else "set",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
