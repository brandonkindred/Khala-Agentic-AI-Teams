"""Non-integration tests for the market-research adapter.

The adapter talks to the Market Research team over HTTP; these tests fake
``httpx.AsyncClient`` (no network, no job service, no Postgres) to exercise the
submit → poll → map flow, the unconfigured short-circuit, and the failure paths.
"""

from __future__ import annotations

from typing import Optional

import pytest

from branding_team.adapters import market_research as mr
from branding_team.models import CompetitiveSnapshot
from branding_team.tests.conftest import make_mission


def _patch_poll(monkeypatch, statuses: list[dict]) -> None:
    """Patch async_get_json to return queued status dicts, one per poll."""
    queue = list(statuses)

    async def _fake_async_get_json(*_a: object, **_kw: object) -> Optional[dict]:
        return queue.pop(0)

    monkeypatch.setattr(mr, "async_get_json", _fake_async_get_json)


def _patch_submit(monkeypatch, result: Optional[dict]) -> None:
    async def _fake_async_post_json(*_a: object, **_kw: object) -> Optional[dict]:
        return result

    monkeypatch.setattr(mr, "async_post_json", _fake_async_post_json)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_build_payload_shapes_request() -> None:
    payload = mr._build_payload(
        make_mission(
            company_name="Acme",
            company_description="A company that builds developer tools",
            target_audience="developers",
            differentiators=["speed", "clarity"],
        )
    )
    assert "Acme" in payload["product_concept"]
    assert payload["target_users"] == "developers"
    assert "speed" in payload["business_goal"]
    assert payload["human_approved"] is True


def test_map_to_competitive_snapshot_extracts_fields() -> None:
    snap = mr._map_to_competitive_snapshot(
        {
            "mission_summary": "summary text",
            "recommendation": {"rationale": ["r1"], "verdict": "go"},
            "insights": [{"pain_points": ["p1", "p2"]}],
            "market_signals": [{"signal": "BrandX"}],
        }
    )
    assert isinstance(snap, CompetitiveSnapshot)
    assert snap.summary == "summary text"
    assert "r1" in snap.insights
    assert "p1" in snap.insights
    assert "BrandX" in snap.similar_brands
    assert snap.source == "market_research_team"


def test_map_to_competitive_snapshot_defaults_summary() -> None:
    snap = mr._map_to_competitive_snapshot({})
    assert snap.summary == "Competitive context requested."


# ---------------------------------------------------------------------------
# request flow
# ---------------------------------------------------------------------------


def test_request_returns_none_when_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("UNIFIED_API_BASE_URL", raising=False)
    monkeypatch.delenv("BRANDING_MARKET_RESEARCH_URL", raising=False)
    assert (
        mr.request_market_research(
            make_mission(
                company_name="Acme",
                company_description="A company that builds developer tools",
                target_audience="developers",
                differentiators=["speed", "clarity"],
            )
        )
        is None
    )


def test_request_success_returns_snapshot(monkeypatch) -> None:
    monkeypatch.setenv("UNIFIED_API_BASE_URL", "http://svc")
    _patch_submit(monkeypatch, {"job_id": "j1"})
    _patch_poll(
        monkeypatch,
        statuses=[{"status": "completed", "result": {"mission_summary": "done"}}],
    )
    snap = mr.request_market_research(
        make_mission(
            company_name="Acme",
            company_description="A company that builds developer tools",
            target_audience="developers",
            differentiators=["speed", "clarity"],
        )
    )
    assert isinstance(snap, CompetitiveSnapshot)
    assert snap.summary == "done"


def test_request_raises_when_no_job_id(monkeypatch) -> None:
    monkeypatch.setenv("UNIFIED_API_BASE_URL", "http://svc")
    _patch_submit(monkeypatch, {})
    with pytest.raises(RuntimeError, match="no job_id"):
        mr.request_market_research(
            make_mission(
                company_name="Acme",
                company_description="A company that builds developer tools",
                target_audience="developers",
                differentiators=["speed", "clarity"],
            )
        )


def test_request_raises_on_terminal_failure(monkeypatch) -> None:
    monkeypatch.setenv("UNIFIED_API_BASE_URL", "http://svc")
    _patch_submit(monkeypatch, {"job_id": "j2"})
    _patch_poll(
        monkeypatch,
        statuses=[{"status": "failed", "error": "boom"}],
    )
    with pytest.raises(RuntimeError, match="ended with status failed"):
        mr.request_market_research(
            make_mission(
                company_name="Acme",
                company_description="A company that builds developer tools",
                target_audience="developers",
                differentiators=["speed", "clarity"],
            )
        )


def test_request_raises_on_timeout(monkeypatch) -> None:
    monkeypatch.setenv("UNIFIED_API_BASE_URL", "http://svc")
    monkeypatch.setenv("BRANDING_MR_TOTAL_TIMEOUT_S", "1")
    monkeypatch.setenv("BRANDING_MR_POLL_INTERVAL_S", "0.1")
    _patch_submit(monkeypatch, {"job_id": "j4"})

    async def _fake_async_get_json(*_a: object, **_kw: object) -> dict:
        return {"status": "running"}

    monkeypatch.setattr(mr, "async_get_json", _fake_async_get_json)

    with pytest.raises(RuntimeError, match="ended with status failed.*Timed out waiting for"):
        mr.request_market_research(
            make_mission(
                company_name="Acme",
                company_description="A company that builds developer tools",
                target_audience="developers",
                differentiators=["speed", "clarity"],
            )
        )


def test_request_wraps_transport_errors(monkeypatch) -> None:
    monkeypatch.setenv("UNIFIED_API_BASE_URL", "http://svc")
    _patch_submit(monkeypatch, None)
    with pytest.raises(RuntimeError, match="Market research request failed"):
        mr.request_market_research(
            make_mission(
                company_name="Acme",
                company_description="A company that builds developer tools",
                target_audience="developers",
                differentiators=["speed", "clarity"],
            )
        )


def test_request_offloads_when_loop_running(monkeypatch) -> None:
    """request_market_research still works when called from a running loop,
    exercising the shared coro_runner.run_coroutine offload path."""
    import asyncio

    monkeypatch.setenv("UNIFIED_API_BASE_URL", "http://svc")
    _patch_submit(monkeypatch, {"job_id": "j3"})
    _patch_poll(
        monkeypatch,
        statuses=[{"status": "completed", "result": {"mission_summary": "offloaded"}}],
    )

    async def _driver():
        # Called synchronously inside a running loop, so request_market_research's
        # internal run_coroutine call must offload to a worker thread instead of
        # calling asyncio.run on the live loop.
        return mr.request_market_research(
            make_mission(
                company_name="Acme",
                company_description="A company that builds developer tools",
                target_audience="developers",
                differentiators=["speed", "clarity"],
            )
        )

    snap = asyncio.run(_driver())
    assert snap.summary == "offloaded"
