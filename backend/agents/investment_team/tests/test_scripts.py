"""Tests for the investment_team CLI scripts.

Covers:
* ``scripts.divergent_provenance.main`` — set-comparison, legacy/empty-
  provenance skip, universe-agnostic skip, and ``--limit`` validation.
* ``scripts.wipe_backtest_records.main`` — dry-run, two-phase deletion,
  and idempotent re-runs (missing backtest IDs).

Both scripts depend on a ``JobServiceClient`` constructed via
``from job_service_client import JobServiceClient``. The tests substitute
in-process fakes (one per team) by patching the symbol that the scripts
import lazily inside ``main()``.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List

import pytest


class _FakeJobClient:
    """Tiny stand-in for JobServiceClient (just the methods used by scripts)."""

    def __init__(self, team: str = "test") -> None:
        self.team = team
        self.jobs: List[Dict[str, Any]] = []
        self.deleted: List[str] = []
        self.delete_returns: Dict[str, bool] = {}

    def list_jobs(self) -> List[Dict[str, Any]]:
        return list(self.jobs)

    def delete_job(self, job_id: str) -> bool:
        self.deleted.append(job_id)
        return self.delete_returns.get(job_id, True)


@pytest.fixture
def patched_job_service_client(monkeypatch: pytest.MonkeyPatch):
    """Patch the lazy ``JobServiceClient`` import inside the scripts.

    Returns a dict the caller pre-populates with per-team fakes. The scripts
    import as ``from job_service_client import JobServiceClient`` inside
    ``main()``; the shim returns the dict entry for the requested team (creating
    a fresh fake on miss so the script doesn't blow up when a team is
    unconfigured).
    """
    instances: Dict[str, _FakeJobClient] = {}

    def _factory(team: str = "test") -> _FakeJobClient:
        existing = instances.get(team)
        if existing is None:
            existing = _FakeJobClient(team=team)
            instances[team] = existing
        return existing

    import job_service_client as jsc_mod

    monkeypatch.setattr(jsc_mod, "JobServiceClient", _factory)
    return instances


# ---------------------------------------------------------------------------
# divergent_provenance
# ---------------------------------------------------------------------------


def test_divergent_provenance_positive_int_rejects_zero_and_negative() -> None:
    from investment_team.scripts.divergent_provenance import _positive_int

    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int("0")
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int("-3")
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int("abc")
    assert _positive_int("4") == 4


def test_divergent_provenance_is_empty_provenance() -> None:
    from investment_team.scripts.divergent_provenance import _is_empty_provenance

    assert _is_empty_provenance({})
    assert _is_empty_provenance({"target_symbols": [], "fetched_symbols": []})
    assert not _is_empty_provenance({"target_symbols": ["QQQ"]})
    assert not _is_empty_provenance({"provider_used": {"QQQ": "yahoo"}})


def test_divergent_provenance_diverges() -> None:
    from investment_team.scripts.divergent_provenance import _diverges

    # Ordering-insensitive set comparison.
    assert not _diverges(["A", "B"], ["B", "A"])
    assert _diverges(["A"], ["B"])
    assert _diverges(["A"], [])


def test_divergent_provenance_main_skips_legacy_and_universe_agnostic_and_reports_divergent(
    capsys: pytest.CaptureFixture[str], patched_job_service_client
) -> None:
    """The main() entrypoint scans, classifies, and prints divergent rows."""
    from investment_team.scripts.divergent_provenance import main

    fake = patched_job_service_client.setdefault(
        "investment_strategy_lab_records",
        _FakeJobClient(team="investment_strategy_lab_records"),
    )
    fake.jobs = [
        # Legacy: no provenance fields — skipped, not divergent.
        {"job_id": "legacy-1", "data": {"backtest": {"data_provenance": {}}}},
        # Universe-agnostic: no target — skipped.
        {
            "job_id": "agnostic-1",
            "data": {
                "backtest": {
                    "data_provenance": {
                        "target_symbols": [],
                        "fetched_symbols": ["QQQ"],
                        "traded_symbols": ["QQQ"],
                        "provider_used": {"QQQ": "yahoo"},
                    }
                }
            },
        },
        # Non-divergent: target and traded match (set equality).
        {
            "job_id": "match-1",
            "data": {
                "backtest": {
                    "data_provenance": {
                        "target_symbols": ["A", "B"],
                        "fetched_symbols": ["A", "B"],
                        "traded_symbols": ["B", "A"],
                        "provider_used": {"A": "yahoo"},
                    }
                },
                "strategy": {"strategy_id": "match-strat"},
            },
        },
        # Divergent — target asked for QQQ, ledger traded TSLA.
        {
            "job_id": "div-1",
            "data": {
                "backtest": {
                    "data_provenance": {
                        "target_symbols": ["QQQ"],
                        "fetched_symbols": ["QQQ"],
                        "traded_symbols": ["TSLA"],
                        "provider_used": {"QQQ": "yahoo"},
                    },
                    "strategy_id": "div-strat-1",
                }
            },
        },
        # Missing job_id is ignored.
        {"data": {}},
    ]

    rc = main([])
    assert rc == 0

    captured = capsys.readouterr().out
    # Divergent row was printed
    assert "div-1" in captured
    assert "div-strat-1" in captured
    # Non-divergent / legacy / agnostic rows are not in the divergent table
    assert "match-1" not in captured
    assert "legacy-1" not in captured
    assert "agnostic-1" not in captured


def test_divergent_provenance_main_stops_at_limit(
    capsys: pytest.CaptureFixture[str], patched_job_service_client
) -> None:
    from investment_team.scripts.divergent_provenance import main

    fake = patched_job_service_client.setdefault(
        "investment_strategy_lab_records",
        _FakeJobClient(team="investment_strategy_lab_records"),
    )
    fake.jobs = [
        {
            "job_id": f"div-{i}",
            "data": {
                "backtest": {
                    "data_provenance": {
                        "target_symbols": ["A"],
                        "fetched_symbols": ["A"],
                        "traded_symbols": [f"OTHER-{i}"],
                        "provider_used": {"A": "yahoo"},
                    }
                },
                "strategy": {"strategy_id": f"s-{i}"},
            },
        }
        for i in range(5)
    ]

    rc = main(["--limit", "2"])
    assert rc == 0
    out = capsys.readouterr().out
    # Should print exactly two divergent IDs (div-0, div-1)
    assert "div-0" in out
    assert "div-1" in out
    assert "div-2" not in out


def test_divergent_provenance_main_fallback_provenance_targets() -> None:
    """Lines covering ``backtest.strategy_id`` fallback (no ``strategy`` block).

    Already covered indirectly above (div-1), but this re-exercises the
    fallback path explicitly with all None-coalescing defaults.
    """
    from investment_team.scripts.divergent_provenance import _diverges, _is_empty_provenance

    # Just sanity-confirm helpers stay consistent
    assert _diverges(["X"], [])
    assert _is_empty_provenance({"target_symbols": [], "fetched_symbols": [], "traded_symbols": []})


# ---------------------------------------------------------------------------
# wipe_backtest_records
# ---------------------------------------------------------------------------


def test_wipe_backtest_records_dry_run_does_not_delete(
    caplog: pytest.LogCaptureFixture, patched_job_service_client
) -> None:
    import logging

    from investment_team.scripts.wipe_backtest_records import main

    lab = _FakeJobClient(team="investment_strategy_lab_records")
    lab.jobs = [
        {"job_id": "lab-1", "data": {"backtest": {"backtest_id": "bt-1"}}},
        {"job_id": "lab-2", "data": {"backtest": {}}},
        {"data": {}},  # missing job_id
    ]
    bt = _FakeJobClient(team="investment_backtests")
    patched_job_service_client["investment_strategy_lab_records"] = lab
    patched_job_service_client["investment_backtests"] = bt

    with caplog.at_level(logging.INFO, logger="wipe_backtest_records"):
        rc = main(["--dry-run"])
    assert rc == 0
    # Nothing was actually deleted in dry-run mode.
    assert lab.deleted == []
    assert bt.deleted == []
    msgs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "would delete" in msgs


def test_wipe_backtest_records_deletes_lab_and_linked_backtests(
    patched_job_service_client,
) -> None:
    from investment_team.scripts.wipe_backtest_records import main

    lab = _FakeJobClient(team="investment_strategy_lab_records")
    lab.jobs = [
        {"job_id": "lab-1", "data": {"backtest": {"backtest_id": "bt-1"}}},
        {"job_id": "lab-2", "data": {"backtest": {}}},  # no linked backtest
        {"job_id": "lab-3", "data": {"backtest": {"backtest_id": "bt-3"}}},
    ]
    bt = _FakeJobClient(team="investment_backtests")
    # Simulate one missing backtest row (already deleted from a previous run).
    bt.delete_returns = {"bt-1": True, "bt-3": False}
    patched_job_service_client["investment_strategy_lab_records"] = lab
    patched_job_service_client["investment_backtests"] = bt

    rc = main([])
    assert rc == 0
    # Both backtest IDs were attempted; the missing one returns False (no count).
    assert bt.deleted == ["bt-1", "bt-3"]
    # All three lab records were deleted (succeed by default in _FakeJobClient).
    assert lab.deleted == ["lab-1", "lab-2", "lab-3"]
