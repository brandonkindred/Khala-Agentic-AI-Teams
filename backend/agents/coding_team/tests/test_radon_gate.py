"""Tests for the Radon CRAP/complexity gate in the coding-team orchestrator.

Covers the defensive env-var helpers and the wiring in ``_run_quality_gates``
that runs Radon on every build and returns a task for revision when complexity
exceeds the configured threshold.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from coding_team.orchestrator import CodingTeamSwarm, _radon_max_cc, _radon_min_mi

# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def test_radon_max_cc_default(monkeypatch) -> None:
    monkeypatch.delenv("RADON_MAX_CC", raising=False)
    assert _radon_max_cc() == 15


@pytest.mark.parametrize("raw", ["abc", "0", "-3"])
def test_radon_max_cc_garbage_or_nonpositive_falls_back(monkeypatch, raw) -> None:
    monkeypatch.setenv("RADON_MAX_CC", raw)
    assert _radon_max_cc() == 15


def test_radon_max_cc_valid(monkeypatch) -> None:
    monkeypatch.setenv("RADON_MAX_CC", "25")
    assert _radon_max_cc() == 25


def test_radon_min_mi_default(monkeypatch) -> None:
    monkeypatch.delenv("RADON_MIN_MI", raising=False)
    assert _radon_min_mi() == 0.0


@pytest.mark.parametrize("raw", ["abc", "-1", "inf", "nan"])
def test_radon_min_mi_garbage_or_invalid_falls_back(monkeypatch, raw) -> None:
    monkeypatch.setenv("RADON_MIN_MI", raw)
    assert _radon_min_mi() == 0.0


def test_radon_min_mi_valid(monkeypatch) -> None:
    monkeypatch.setenv("RADON_MIN_MI", "20")
    assert _radon_min_mi() == 20.0


# ---------------------------------------------------------------------------
# _run_quality_gates Radon wiring
# ---------------------------------------------------------------------------


def _make_swarm(tmp_path):
    """Build a CodingTeamSwarm with only the attributes the gate touches."""
    swarm = object.__new__(CodingTeamSwarm)
    swarm.path = tmp_path
    swarm.graph = MagicMock()
    swarm.llm_getter = lambda key: MagicMock()
    return swarm


def _make_task():
    return SimpleNamespace(
        id="t1",
        title="Add feature",
        description="desc",
        acceptance_criteria=[],
        revision_count=0,
        revision_feedback=None,
    )


def _patch_quality_tools(monkeypatch, *, radon_passed, build_ok=True):
    """Patch the quality_gate_tools symbols imported inside _run_quality_gates."""
    qgt = "software_engineering_team.quality_gate_tools"
    monkeypatch.setattr(
        f"{qgt}.run_build_verification",
        lambda path, agent_type, task_id: SimpleNamespace(success=build_ok, error="boom"),
    )
    monkeypatch.setattr(
        f"{qgt}.run_radon",
        lambda path, agent_type, task_id, **kw: SimpleNamespace(
            passed=radon_passed,
            summary="too complex" if not radon_passed else "",
            violations=[] if radon_passed else [{"metric": "cc"}],
        ),
    )
    monkeypatch.setattr(
        f"{qgt}.run_linting", lambda path, task_id, **kw: SimpleNamespace(passed=True)
    )
    monkeypatch.setattr(
        f"{qgt}.run_code_review",
        lambda **kw: SimpleNamespace(approved=True, issues=[]),
    )


def test_quality_gates_blocks_on_radon_violation(monkeypatch, tmp_path) -> None:
    _patch_quality_tools(monkeypatch, radon_passed=False)
    captured = {}

    def fake_return(self, task, feedback):
        captured["feedback"] = feedback
        return False

    monkeypatch.setattr(CodingTeamSwarm, "_return_for_revision", fake_return)

    swarm = _make_swarm(tmp_path)
    swe = SimpleNamespace(agent_id="a1", stack_spec=SimpleNamespace(name="backend"))
    ok = swarm._run_quality_gates(swe, _make_task(), {"changes_summary": ""}, MagicMock())

    assert ok is False
    assert captured["feedback"][0]["type"] == "complexity"
    assert "too complex" in captured["feedback"][0]["error"]


def test_quality_gates_passes_when_radon_clean(monkeypatch, tmp_path) -> None:
    _patch_quality_tools(monkeypatch, radon_passed=True)
    # Code review approves, so the gate should reach the end and return True.
    swarm = _make_swarm(tmp_path)
    swe = SimpleNamespace(agent_id="a1", stack_spec=SimpleNamespace(name="backend"))
    ok = swarm._run_quality_gates(swe, _make_task(), {"changes_summary": ""}, MagicMock())
    assert ok is True
