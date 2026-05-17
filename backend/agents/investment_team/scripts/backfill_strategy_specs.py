"""Backfill persisted ``StrategySpec`` JSON to the issue #537 schema.

Run once before deploying the strict-schema commit.

Per-team payload shapes (the script handles each correctly):
  - ``investment_strategies``           — ``data`` IS the
    ``StrategySpec.model_dump()`` payload (via
    ``_PersistentDict.__setitem__`` in ``api/main.py`` — no nested
    ``strategy`` key).
  - ``investment_backtests``            — ``data`` is a
    ``BacktestRecord`` with the spec nested at ``data["strategy"]``.
  - ``investment_strategy_lab_records`` — ``data`` is a
    ``StrategyLabRecord`` with the spec at ``data["strategy"]`` AND
    ``data["original_spec"]`` AND ``data["backtest"]["strategy"]``;
    all three are migrated.

For each spec we round-trip the JSON through
``StrategySpec.model_validate(...)`` — which runs the in-flight legacy
rewriter — then write the canonicalised JSON back. The backfill is
deliberately **permissive** about a missing ``timeframe``: persisted
pre-#537 records that lack any structural legacy markers (e.g. empty
``entry_rules``/``exit_rules`` with default sizing) bypass the runtime
``_migrate_legacy_payload`` heuristic, so the script injects
``timeframe="1d"`` defensively before validating. Fresh callers
go through the strict ``CreateStrategyRequest`` contract, which still
requires the field.

Rows that fail validation for any other reason are reported and
skipped; rerun after fixing them by hand.

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
from typing import Any

logger = logging.getLogger("backfill_strategy_specs")


_TEAMS = (
    "investment_strategy_lab_records",
    "investment_backtests",
    "investment_strategies",
)

_DEFAULT_TIMEFRAME = "1d"


def _migrate_spec_payload(raw: Any) -> tuple[bool, Any]:
    """Migrate one ``StrategySpec``-shaped dict.

    Returns ``(changed, new_spec_dict)``. Permissive about missing
    ``timeframe``: when the field is absent we inject ``"1d"`` before
    validation, since the runtime ``_migrate_legacy_payload`` heuristic
    only defaults timeframe when other structural legacy markers are
    present.
    """
    from investment_team.models import StrategySpec

    if not isinstance(raw, dict):
        return False, raw

    raw_with_tf: dict = dict(raw)
    raw_with_tf.setdefault("timeframe", _DEFAULT_TIMEFRAME)

    migrated = StrategySpec.model_validate(raw_with_tf)
    new_spec = migrated.model_dump(mode="json")
    if new_spec == raw:
        return False, raw
    return True, new_spec


def _migrate_one(team: str, payload: dict) -> tuple[bool, dict]:
    """Per-team payload migration. Returns ``(changed, new_payload)``."""
    if not isinstance(payload, dict):
        return False, payload

    if team == "investment_strategies":
        # ``_PersistentDict("strategies").__setitem__`` writes
        # ``StrategySpec.model_dump()`` straight into ``data`` — no
        # nested ``strategy`` key. Treat the whole payload as the spec.
        if not payload.get("strategy_id"):
            return False, payload
        return _migrate_spec_payload(payload)

    if team == "investment_backtests":
        raw = payload.get("strategy")
        changed, new_spec = _migrate_spec_payload(raw)
        if not changed:
            return False, payload
        new_payload = dict(payload)
        new_payload["strategy"] = new_spec
        return True, new_payload

    if team == "investment_strategy_lab_records":
        # The lab record nests the spec in three places — all three are
        # in scope so a partial migration doesn't leave the record
        # internally inconsistent.
        any_changed = False
        new_payload = dict(payload)

        for key in ("strategy", "original_spec"):
            spec_raw = new_payload.get(key)
            if isinstance(spec_raw, dict):
                changed, new_spec = _migrate_spec_payload(spec_raw)
                if changed:
                    new_payload[key] = new_spec
                    any_changed = True

        bt = new_payload.get("backtest")
        if isinstance(bt, dict):
            bt_strategy = bt.get("strategy")
            if isinstance(bt_strategy, dict):
                changed, new_spec = _migrate_spec_payload(bt_strategy)
                if changed:
                    new_bt = dict(bt)
                    new_bt["strategy"] = new_spec
                    new_payload["backtest"] = new_bt
                    any_changed = True

        return any_changed, new_payload

    return False, payload


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
                changed, new_data = _migrate_one(team, data)
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
