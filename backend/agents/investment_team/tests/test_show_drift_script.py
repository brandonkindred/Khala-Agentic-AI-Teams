"""Tests for the show_drift CLI script."""

from __future__ import annotations

from typing import Any, Dict
from unittest import mock

import pytest


def _synthetic_record() -> Dict[str, Any]:
    return {
        "lab_record_id": "lab-abc123",
        "strategy": {"strategy_id": "strat-1"},
        "is_winning": True,
        "created_at": "2024-01-01T00:00:00+00:00",
        "spec_history": [
            {
                "phase": "design_review",
                "agent": "DesignAgent",
                "timestamp": "2024-01-01T00:00:01+00:00",
                "before_hash": "a" * 64,
                "after_hash": "b" * 64,
                "diff": "--- before\n+++ after\n-old\n+new\n",
                "reason": "revised hypothesis",
                "gate_failures": [],
            },
        ],
        "code_history": [
            {
                "phase": "synthesis",
                "agent": "compiler",
                "timestamp": "2024-01-01T00:00:02+00:00",
                "before_hash": "c" * 64,
                "after_hash": "d" * 64,
                "diff": "--- before\n+++ after\n",
                "reason": "initial code synthesis",
                "gate_failures": [],
            },
        ],
        "gate_timeline": [
            {
                "phase": "design",
                "gate_name": "spec_readiness",
                "passed": True,
                "severity": "info",
                "details": "ok",
                "timestamp": "2024-01-01T00:00:00+00:00",
            },
        ],
        "rule_implementation_map": [
            {"rule_id": "entry[0]", "code_line_refs": [], "traded_count": 5},
            {"rule_id": "sizing", "code_line_refs": [], "traded_count": 0},
        ],
    }


class TestShowDrift:
    def test_prints_spec_diff(self, capsys: pytest.CaptureFixture[str]):
        from investment_team.scripts.show_drift import (
            _print_spec_history,
        )

        record = _synthetic_record()
        _print_spec_history(record["spec_history"])
        captured = capsys.readouterr().out
        assert "DesignAgent" in captured
        assert "revised hypothesis" in captured

    def test_prints_zero_trade_marker(self, capsys: pytest.CaptureFixture[str]):
        from investment_team.scripts.show_drift import _print_rule_map

        record = _synthetic_record()
        _print_rule_map(record["rule_implementation_map"])
        captured = capsys.readouterr().out
        assert "ZERO TRADES" in captured
        assert "entry[0]" in captured

    def test_main_record_not_found(self):
        from investment_team.scripts.show_drift import main

        mock_client = mock.MagicMock()
        mock_client.list_jobs.return_value = []

        with mock.patch("job_service_client.JobServiceClient", return_value=mock_client):
            with pytest.raises(SystemExit) as exc_info:
                main(["lab-nonexistent"])
            assert exc_info.value.code == 1

    def test_main_success(self, capsys: pytest.CaptureFixture[str]):
        from investment_team.scripts.show_drift import main

        mock_client = mock.MagicMock()
        mock_client.list_jobs.return_value = [{"data": _synthetic_record()}]

        with mock.patch("job_service_client.JobServiceClient", return_value=mock_client):
            main(["lab-abc123"])

        captured = capsys.readouterr().out
        assert "Drift Report" in captured
        assert "lab-abc123" in captured
        assert "DesignAgent" in captured

    def test_empty_histories(self, capsys: pytest.CaptureFixture[str]):
        from investment_team.scripts.show_drift import (
            _print_code_history,
            _print_gate_timeline,
            _print_rule_map,
            _print_spec_history,
        )

        _print_spec_history([])
        _print_code_history([])
        _print_gate_timeline([])
        _print_rule_map([])
        captured = capsys.readouterr().out
        assert "no spec revisions" in captured
        assert "no code revisions" in captured
        assert "no gate events" in captured
        assert "no rule implementation map" in captured
