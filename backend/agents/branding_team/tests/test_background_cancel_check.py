"""Cooperative-cancellation wiring for ``branding_team/api/background.py``.

``_run_branding_core`` hands ``orchestrator.run`` a ``should_continue`` gate so a
cancel requested mid-run stops the phase loop instead of letting every remaining
phase burn its LLM fan-out. These tests pin both halves of that wiring: the
``_job_not_cancelled`` adapter's fail-open contract, and the fact that the gate
actually reaches the orchestrator and tracks live job state.

Patch target matters: ``background`` binds ``is_job_cancelled`` into its own module
namespace at import, so patching ``branding_team.shared.job_store.is_job_cancelled``
would not be observed here. These tests patch ``background.is_job_cancelled``.

No live Postgres or job service needed -- runs in the default suite.
"""

from __future__ import annotations

import logging

import pytest

from branding_team.api import background as bg
from branding_team.api import main as api_main
from branding_team.shared import job_store


class _Result:
    def model_dump(self):
        return {}


class _CapturingOrchestrator:
    """Fake orchestrator that records the kwargs ``_run_branding_core`` passes."""

    def __init__(self) -> None:
        self.captured: dict = {}

    def run(self, **kwargs):
        self.captured = kwargs
        return _Result()


@pytest.fixture
def captured_should_continue(monkeypatch):
    """Run ``_run_branding_core`` once and yield the ``should_continue`` it passed."""
    orchestrator = _CapturingOrchestrator()
    monkeypatch.setattr(api_main, "orchestrator", orchestrator)
    monkeypatch.setattr(api_main, "_job_manager", None)
    monkeypatch.setattr(job_store, "update_job_if_not_cancelled", lambda *a, **kw: True)

    api_main._run_branding_core(
        job_id="job-cancel",
        mission=object(),
        human_review=object(),
        brand_checks=[],
        client_id=None,
        brand_id=None,
        include_market_research=False,
        include_design_assets=False,
        target_phase=None,
    )

    assert "should_continue" in orchestrator.captured, (
        "_run_branding_core must pass should_continue into orchestrator.run"
    )
    return orchestrator.captured["should_continue"]


def test_job_not_cancelled_is_false_for_a_cancelled_job(monkeypatch) -> None:
    monkeypatch.setattr(bg, "is_job_cancelled", lambda job_id: True)
    assert bg._job_not_cancelled("job-1") is False


def test_job_not_cancelled_is_true_for_a_live_job(monkeypatch) -> None:
    monkeypatch.setattr(bg, "is_job_cancelled", lambda job_id: False)
    assert bg._job_not_cancelled("job-1") is True


def test_job_not_cancelled_fails_open_when_the_probe_raises(monkeypatch, caplog) -> None:
    """A job-service blip must not abort an otherwise healthy multi-minute run."""

    def _boom(job_id):
        raise RuntimeError("transport blip")

    monkeypatch.setattr(bg, "is_job_cancelled", _boom)

    with caplog.at_level(logging.WARNING, logger=bg.__name__):
        assert bg._job_not_cancelled("job-1") is True

    assert "cancel probe failed" in caplog.text


def test_should_continue_tracks_live_cancellation(monkeypatch, captured_should_continue) -> None:
    """The gate handed to the orchestrator reflects job state at call time."""
    probed: list[str] = []

    def _probe(job_id):
        probed.append(job_id)
        return len(probed) > 1  # not cancelled on the first call, cancelled after

    monkeypatch.setattr(bg, "is_job_cancelled", _probe)

    assert captured_should_continue() is True
    assert captured_should_continue() is False
    assert probed == ["job-cancel", "job-cancel"], "the gate must probe this run's own job_id"


def test_should_continue_fails_open_when_the_probe_raises(
    monkeypatch, captured_should_continue
) -> None:
    def _boom(job_id):
        raise RuntimeError("transport blip")

    monkeypatch.setattr(bg, "is_job_cancelled", _boom)

    assert captured_should_continue() is True
