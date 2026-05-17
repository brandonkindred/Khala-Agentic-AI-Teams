"""Backfill persisted ``StrategySpec`` JSON to the issue #537 schema.

Run once before deploying the strict-schema commit.

The job-service teams that hold ``StrategySpec`` payloads are:
  - ``investment_strategy_lab_records`` — each lab record carries a
    ``strategy`` field shaped like ``StrategySpec.model_dump()``.
  - ``investment_backtests``            — each backtest carries a
    ``strategy`` field with the same shape.
  - ``investment_strategies``           — strategies persisted via
    ``POST /strategies``.

For each row we round-trip ``data["strategy"]`` through
``StrategySpec.model_validate(...)`` — which runs the in-flight legacy
rewriter — then write the canonicalised JSON back. Rows that fail
validation (genuinely malformed payloads, not just schema-version drift)
are reported and skipped; rerun after fixing them by hand.

Usage::

    PYTHONPATH=agents python3 -m investment_team.scripts.backfill_strategy_specs [--dry-run] [--limit N]

Requires ``JOB_SERVICE_URL`` to be set (same env var as the running API).

The script is idempotent: re-running on already-migrated rows is a no-op
(canonical JSON in = canonical JSON out).
"""

from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger("backfill_strategy_specs")


_TEAMS = (
    "investment_strategy_lab_records",
    "investment_backtests",
    "investment_strategies",
)


def _migrate_one(payload: dict) -> tuple[bool, dict]:
    """Round-trip a job-service payload's ``strategy`` field through StrategySpec.

    Returns ``(changed, new_payload)``. ``changed`` is True only when the
    canonicalised JSON differs from the original — so idempotent runs are
    safe.
    """
    from investment_team.models import StrategySpec

    if not isinstance(payload, dict):
        return False, payload
    raw = payload.get("strategy")
    if not isinstance(raw, dict):
        return False, payload

    migrated = StrategySpec.model_validate(raw)
    new_strategy = migrated.model_dump(mode="json")
    if new_strategy == raw:
        return False, payload

    new_payload = dict(payload)
    new_payload["strategy"] = new_strategy
    return True, new_payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Run the migration without writing back"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of rows to migrate per team (0 = all).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from job_service_client import JobServiceClient

    total_changed = 0
    total_unchanged = 0
    total_failed = 0

    for team in _TEAMS:
        client = JobServiceClient(team=team)
        jobs = client.list_jobs() or []
        logger.info("%s: %d job(s) found", team, len(jobs))
        n_changed = 0
        n_unchanged = 0
        n_failed = 0
        for i, job in enumerate(jobs):
            if args.limit and i >= args.limit:
                break
            jid = job.get("job_id")
            data = job.get("data") or {}
            if not jid:
                continue
            try:
                changed, new_data = _migrate_one(data)
            except Exception as exc:  # noqa: BLE001
                n_failed += 1
                logger.warning("%s/%s migration failed: %s", team, jid, exc)
                continue

            if not changed:
                n_unchanged += 1
                continue

            if args.dry_run:
                logger.info("[dry-run] would rewrite %s/%s", team, jid)
            else:
                client.update_job(jid, data=new_data)
                logger.info("rewrote %s/%s", team, jid)
            n_changed += 1

        logger.info("%s: changed=%d unchanged=%d failed=%d", team, n_changed, n_unchanged, n_failed)
        total_changed += n_changed
        total_unchanged += n_unchanged
        total_failed += n_failed

    logger.info(
        "done — total changed=%d unchanged=%d failed=%d%s",
        total_changed,
        total_unchanged,
        total_failed,
        " (dry run)" if args.dry_run else "",
    )
    return 0 if total_failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
