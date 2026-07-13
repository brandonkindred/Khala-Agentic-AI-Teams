"""Unit tests for ``pipeline.py``'s env-driven pipeline timeout.

The happy-path RUNNING→COMPLETED flow is already covered end-to-end by
``test_api.py``; these focus on the timeout ceiling added to bound a stuck
orchestrator run, which nothing else in this suite covers.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import pytest

from market_research_team import pipeline as mr_pipeline
from market_research_team.models import HumanReview, ResearchMission, TeamTopology
from market_research_team.shared import job_store as js


def test_pipeline_timeout_s_default_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RESEARCH_PIPELINE_TIMEOUT_S", raising=False)
    assert mr_pipeline._pipeline_timeout_s() == mr_pipeline._DEFAULT_PIPELINE_TIMEOUT_S


def test_pipeline_timeout_s_clamped_to_floor(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_RESEARCH_PIPELINE_TIMEOUT_S", "1")
    assert mr_pipeline._pipeline_timeout_s() == 30.0


def test_pipeline_timeout_s_clamped_to_ceiling(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_RESEARCH_PIPELINE_TIMEOUT_S", "999999")
    assert mr_pipeline._pipeline_timeout_s() == mr_pipeline._MAX_PIPELINE_TIMEOUT_S


def test_pipeline_timeout_s_garbage_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_RESEARCH_PIPELINE_TIMEOUT_S", "not-a-number")
    assert mr_pipeline._pipeline_timeout_s() == mr_pipeline._DEFAULT_PIPELINE_TIMEOUT_S


def test_run_pipeline_core_raises_timeout_without_blocking_on_slow_orchestrator(
    monkeypatch, fake_job_client
) -> None:
    """Regression: a prior version wrapped the orchestrator call in
    ``with ThreadPoolExecutor(...) as pool:`` — the context manager's
    ``__exit__`` calls ``shutdown(wait=True)``, which blocks until the
    orchestrator thread actually finishes, defeating the timeout entirely.
    The caller's wait must be bounded by the configured timeout regardless of
    how long the orchestrator thread keeps running in the background."""
    monkeypatch.setattr(mr_pipeline, "_pipeline_timeout_s", lambda: 0.1)

    class _SlowOrchestrator:
        def run(self, *_args: Any, **_kwargs: Any) -> Any:
            time.sleep(2.0)

    monkeypatch.setattr(mr_pipeline, "MarketResearchOrchestrator", _SlowOrchestrator)

    job_id = str(uuid4())
    fake_job_client.create_job(job_id, status=js.JOB_STATUS_PENDING)
    mission = ResearchMission(
        product_concept="Slow run",
        target_users="x",
        business_goal="y",
        topology=TeamTopology.UNIFIED,
    )

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="exceeded"):
        mr_pipeline.run_pipeline_core(job_id, mission, HumanReview(approved=True))
    elapsed = time.monotonic() - started

    # Bounded by the 0.1s timeout, not the orchestrator's 2s sleep.
    assert elapsed < 1.0
