"""Unit tests for :class:`ProductOwnerAgent` with a stub LLM client."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from product_delivery.models import Story
from product_delivery.product_owner_agent import ProductOwnerAgent


class _StubLLM:
    """Returns a canned ``complete_json`` response."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt})
        return self.payload


class _FakeStore:
    """In-memory subset of ``ProductDeliveryStore`` used by the agent."""

    def __init__(self, stories: list[Story]) -> None:
        self._stories = stories
        self.persisted: list[tuple[str, float | None, float | None]] = []

    def list_stories_for_product(self, product_id: str) -> list[Story]:
        return [s for s in self._stories if s.id.startswith(product_id) or True]

    def bulk_update_story_scores(
        self,
        rows: list[tuple[str, float | None, float | None]],
    ) -> int:
        rows = list(rows)
        self.persisted.extend(rows)
        return len(rows)


def _story(sid: str, *, title: str = "do thing", points: float | None = 5.0) -> Story:
    now = datetime.now(tz=timezone.utc)
    return Story(
        id=sid,
        epic_id="epic1",
        title=title,
        user_story="",
        status="proposed",
        wsjf_score=None,
        rice_score=None,
        estimate_points=points,
        author="tester",
        created_at=now,
        updated_at=now,
    )


def test_groom_wsjf_ranks_and_persists_scores() -> None:
    stories = [_story("s1", title="alpha"), _story("s2", title="beta")]
    llm = _StubLLM(
        payload={
            "items": [
                {
                    "id": "s1",
                    "inputs": {
                        "user_business_value": 5,
                        "time_criticality": 3,
                        "risk_reduction_or_opportunity_enablement": 2,
                        "job_size": 5,
                    },
                    "rationale": "important",
                },
                {
                    "id": "s2",
                    "inputs": {
                        "user_business_value": 9,
                        "time_criticality": 6,
                        "risk_reduction_or_opportunity_enablement": 5,
                        "job_size": 5,
                    },
                    "rationale": "critical",
                },
            ]
        }
    )
    store = _FakeStore(stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]

    result = agent.groom(product_id="anything", method="wsjf", persist=True)

    # Sorted descending by score: s2 (4.0) then s1 (2.0)
    assert [r.id for r in result.ranked] == ["s2", "s1"]
    assert result.ranked[0].score == 4.0
    assert result.ranked[1].score == 2.0
    # persistence ran
    assert {r[0] for r in store.persisted} == {"s1", "s2"}
    # LLM was called exactly once with the system prompt set
    assert len(llm.calls) == 1
    assert llm.calls[0]["system_prompt"]


def test_groom_rice_uses_estimate_points_when_effort_missing() -> None:
    stories = [_story("s1", points=8.0)]
    llm = _StubLLM(
        payload={
            "items": [
                {
                    "id": "s1",
                    "inputs": {"reach": 1000, "impact": 1, "confidence": 1},
                    "rationale": "",
                }
            ]
        }
    )
    store = _FakeStore(stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="x", method="rice", persist=False)
    # effort defaults to estimate_points / 4 = 2.0 → score 500
    assert result.ranked[0].score == 500.0
    assert store.persisted == []


def test_groom_skips_unknown_story_ids() -> None:
    stories = [_story("real")]
    llm = _StubLLM(
        payload={
            "items": [
                {"id": "ghost", "inputs": {}, "rationale": ""},
                {
                    "id": "real",
                    "inputs": {
                        "user_business_value": 1,
                        "time_criticality": 1,
                        "risk_reduction_or_opportunity_enablement": 1,
                        "job_size": 3,
                    },
                    "rationale": "",
                },
            ]
        }
    )
    store = _FakeStore(stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="x", method="wsjf", persist=False)
    assert [r.id for r in result.ranked] == ["real"]


def test_groom_returns_empty_when_backlog_empty() -> None:
    store = _FakeStore([])
    llm = _StubLLM(payload={"items": []})
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="x", method="wsjf")
    assert result.ranked == []
    # LLM should not have been called when backlog is empty
    assert llm.calls == []


def test_groom_handles_llm_exception_gracefully() -> None:
    class _BoomLLM:
        def complete_json(self, *a: Any, **kw: Any) -> dict[str, Any]:
            raise RuntimeError("model unreachable")

    stories = [_story("s1")]
    store = _FakeStore(stories)
    agent = ProductOwnerAgent(store=store, llm_client=_BoomLLM())  # type: ignore[arg-type]
    result = agent.groom(product_id="x", method="wsjf")
    assert result.ranked == []


def test_groom_wsjf_treats_null_job_size_as_estimate_points_fallback() -> None:
    # The model occasionally emits explicit `null` for job_size when it
    # can't size a story. Without normalization, float(None) trips the
    # exception handler and silently drops the story from the ranking.
    stories = [_story("s1", points=4.0)]
    llm = _StubLLM(
        payload={
            "items": [
                {
                    "id": "s1",
                    "inputs": {
                        "user_business_value": 6,
                        "time_criticality": 4,
                        "risk_reduction_or_opportunity_enablement": 2,
                        "job_size": None,
                    },
                    "rationale": "",
                }
            ]
        }
    )
    store = _FakeStore(stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="x", method="wsjf", persist=False)
    # cost_of_delay = 12; job_size falls back to estimate_points (4) → 3.0
    assert len(result.ranked) == 1
    assert result.ranked[0].score == 3.0


def test_groom_rice_treats_null_effort_as_estimate_points_fallback() -> None:
    stories = [_story("s1", points=8.0)]
    llm = _StubLLM(
        payload={
            "items": [
                {
                    "id": "s1",
                    "inputs": {
                        "reach": 1000,
                        "impact": 1,
                        "confidence": 1,
                        "effort": None,
                    },
                    "rationale": "",
                }
            ]
        }
    )
    store = _FakeStore(stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="x", method="rice", persist=False)
    # effort falls back to estimate_points / 4 = 2.0 → score 500
    assert len(result.ranked) == 1
    assert result.ranked[0].score == 500.0


def test_groom_wsjf_treats_null_value_components_as_zero() -> None:
    # All non-denominator inputs default to 0 when the model emits null.
    stories = [_story("s1", points=2.0)]
    llm = _StubLLM(
        payload={
            "items": [
                {
                    "id": "s1",
                    "inputs": {
                        "user_business_value": None,
                        "time_criticality": None,
                        "risk_reduction_or_opportunity_enablement": None,
                        "job_size": 4,
                    },
                    "rationale": "",
                }
            ]
        }
    )
    store = _FakeStore(stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="x", method="wsjf", persist=False)
    # cost_of_delay = 0 → score 0
    assert len(result.ranked) == 1
    assert result.ranked[0].score == 0.0
