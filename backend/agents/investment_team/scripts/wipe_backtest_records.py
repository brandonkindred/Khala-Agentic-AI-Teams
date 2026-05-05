"""Wipe persisted BacktestRecord rows from the job service.

Targets two job-service teams:
  - ``investment_strategy_lab_records`` — all lab run cards.
  - ``investment_backtests``            — only rows whose job_id starts with
                                         ``bt-lab-`` (lab-originated runs).

Rows in ``investment_backtests`` created via ``POST /backtests`` from outside
the lab are preserved.

Run once before deploying the schema-tightening commit (issue #432).
Run from ``backend/`` (same directory as ``Makefile``)::

    PYTHONPATH=agents python3 -m investment_team.scripts.wipe_backtest_records [--dry-run]

Requires ``JOB_SERVICE_URL`` to be set (same env var as the running API).
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

    deleted_lab_records = 0
    deleted_lab_backtests = 0

    lab_client = JobServiceClient(team="investment_strategy_lab_records")
    for job in lab_client.list_jobs() or []:
        jid = job.get("job_id")
        if not jid:
            continue
        if args.dry_run:
            logger.info("[dry-run] would delete lab record %s", jid)
        elif lab_client.delete_job(str(jid)):
            deleted_lab_records += 1
            logger.info("deleted lab record %s", jid)

    bt_client = JobServiceClient(team="investment_backtests")
    for job in bt_client.list_jobs() or []:
        jid = str(job.get("job_id") or "")
        if not jid.startswith("bt-lab-"):
            continue
        if args.dry_run:
            logger.info("[dry-run] would delete backtest %s", jid)
        elif bt_client.delete_job(jid):
            deleted_lab_backtests += 1
            logger.info("deleted backtest %s", jid)

    logger.info(
        "done — lab_records=%d, lab_backtests=%d%s",
        deleted_lab_records,
        deleted_lab_backtests,
        " (dry run)" if args.dry_run else "",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
