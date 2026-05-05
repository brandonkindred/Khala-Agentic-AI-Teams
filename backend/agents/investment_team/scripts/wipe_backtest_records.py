"""Wipe persisted BacktestRecord rows from the job service.

Targets two job-service teams:
  - ``investment_strategy_lab_records`` — all lab run cards.
  - ``investment_backtests``            — only rows whose backtest_id was
                                         linked from a lab run card (collected
                                         from the lab record payload before
                                         deletion).

Rows in ``investment_backtests`` created via ``POST /backtests`` from outside
the lab are preserved.

Run once before deploying the schema-tightening commit (issue #432).
Run from ``backend/`` (same directory as ``Makefile``)::

    PYTHONPATH=agents python3 -m investment_team.scripts.wipe_backtest_records [--dry-run]

Requires ``JOB_SERVICE_URL`` to be set (same env var as the running API).

Deletion order (safe to interrupt and retry):
  1. Collect all (lab_record_id, backtest_id) pairs without deleting anything.
  2. Delete linked backtest rows from ``investment_backtests``.
  3. Delete lab record rows from ``investment_strategy_lab_records``.

If interrupted between steps 2 and 3, rerunning will re-collect IDs from the
surviving lab records, skip the already-deleted backtest rows (idempotent),
and finish deleting the lab records.
"""

from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger("wipe_backtest_records")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print what would be deleted without deleting")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from job_service_client import JobServiceClient

    lab_client = JobServiceClient(team="investment_strategy_lab_records")
    bt_client = JobServiceClient(team="investment_backtests")

    # Pass 1: collect without deleting so the script is safely restartable.
    pairs: list[tuple[str, str | None]] = []
    for job in lab_client.list_jobs() or []:
        jid = job.get("job_id")
        if not jid:
            continue
        payload = job.get("data") or {}
        bt_id: str | None = (payload.get("backtest") or {}).get("backtest_id")
        pairs.append((str(jid), bt_id))

    logger.info("found %d lab record(s) to wipe", len(pairs))

    # Pass 2: delete linked backtest rows first.
    deleted_lab_backtests = 0
    for _lab_id, bt_id in pairs:
        if not bt_id:
            continue
        if args.dry_run:
            logger.info("[dry-run] would delete backtest %s", bt_id)
        elif bt_client.delete_job(bt_id):
            deleted_lab_backtests += 1
            logger.info("deleted backtest %s", bt_id)

    # Pass 3: delete lab records only after their backtests are gone.
    deleted_lab_records = 0
    for lab_id, bt_id in pairs:
        if args.dry_run:
            logger.info("[dry-run] would delete lab record %s (linked backtest: %s)", lab_id, bt_id or "none")
        elif lab_client.delete_job(lab_id):
            deleted_lab_records += 1
            logger.info("deleted lab record %s (linked backtest: %s)", lab_id, bt_id or "none")

    logger.info(
        "done — lab_records=%d, lab_backtests=%d%s",
        deleted_lab_records,
        deleted_lab_backtests,
        " (dry run)" if args.dry_run else "",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
