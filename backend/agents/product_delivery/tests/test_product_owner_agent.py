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
    """In-memory subset of ``ProductDeliveryStore`` used by the agent.

    Stories are keyed by ``product_id`` so tests can put two products
    side by side and assert that ``list_stories_for_product`` only
    returns the stories scoped to the requested product (i.e. catches
    a regression that would leak the agent across product boundaries).
    """

    def __init__(self, stories_by_product: dict[str, list[Story]]) -> None:
        self._stories_by_product = stories_by_product
        self.persisted: list[tuple[str, float | None, float | None]] = []

    def list_stories_for_product(self, product_id: str) -> list[Story]:
        return list(self._stories_by_product.get(product_id, []))

    def bulk_update_story_scores(
        self,
        rows: list[tuple[str, float | None, float | None]],
    ) -> int:
        rows = list(rows)
        self.persisted.extend(rows)
        return len(rows)


def _fake_store(*, stories: list[Story], product_id: str = "prod-x") -> _FakeStore:
    """Most tests use a single product; this keeps the call sites short."""
    return _FakeStore({product_id: stories})


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
    store = _fake_store(stories=stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]

    result = agent.groom(product_id="prod-x", method="wsjf", persist=True)

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
    store = _fake_store(stories=stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="prod-x", method="rice", persist=False)
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
    store = _fake_store(stories=stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="prod-x", method="wsjf", persist=False)
    assert [r.id for r in result.ranked] == ["real"]


def test_groom_returns_empty_when_backlog_empty() -> None:
    store = _fake_store(stories=[])
    llm = _StubLLM(payload={"items": []})
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="prod-x", method="wsjf")
    assert result.ranked == []
    # LLM should not have been called when backlog is empty
    assert llm.calls == []


def test_groom_raises_llm_unavailable_on_end_to_end_llm_failure() -> None:
    # End-to-end LLM failure (transport / model / parse) must surface as
    # `LLMScoringUnavailable` so the route can return 503 and callers
    # retry. Returning 200 with every story zero-scored would silently
    # de-prioritise the backlog during a transient outage. (Per-story
    # missing/malformed payloads are handled gracefully via the
    # fallback path — see `test_groom_includes_stories_omitted_by_llm`.)
    import pytest as _pytest

    from product_delivery.product_owner_agent.agent import LLMScoringUnavailable

    class _BoomLLM:
        def complete_json(self, *a: Any, **kw: Any) -> dict[str, Any]:
            raise RuntimeError("model unreachable")

    stories = [_story("s1")]
    store = _fake_store(stories=stories)
    agent = ProductOwnerAgent(store=store, llm_client=_BoomLLM())  # type: ignore[arg-type]
    with _pytest.raises(LLMScoringUnavailable):
        agent.groom(product_id="prod-x", method="wsjf")


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
    store = _fake_store(stories=stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="prod-x", method="wsjf", persist=False)
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
    store = _fake_store(stories=stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="prod-x", method="rice", persist=False)
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
    store = _fake_store(stories=stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="prod-x", method="wsjf", persist=False)
    # cost_of_delay = 0 → score 0
    assert len(result.ranked) == 1
    assert result.ranked[0].score == 0.0


def test_groom_treats_non_finite_inputs_as_fallback() -> None:
    # Models occasionally emit `"NaN"` or `"Infinity"` for fields they
    # refuse to commit to. `float("NaN")` succeeds but propagates into
    # `RankedBacklogItem.score`, breaking JSON serialization downstream.
    # `_to_float` must fall back the same way it does for None.
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
                        # Non-finite denominator — must fall back to
                        # estimate_points (4) rather than become inf.
                        "job_size": float("inf"),
                    },
                    "rationale": "",
                }
            ]
        }
    )
    store = _fake_store(stories=stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="prod-x", method="wsjf", persist=False)
    assert len(result.ranked) == 1
    # cost_of_delay = 12; job_size falls back to estimate_points (4) → 3.0.
    assert result.ranked[0].score == 3.0
    # And the score is finite for the JSON encoder downstream.
    import math

    assert math.isfinite(result.ranked[0].score)


def test_groom_surfaces_malformed_inputs_with_score_zero() -> None:
    # When an LLM returns un-coerceable values (e.g. a list where a
    # number is expected), the agent must still surface the story in
    # ``GroomResult.ranked`` with score=0 + a malformed-inputs rationale.
    # Dropping it silently is the failure mode flagged by review.
    stories = [_story("s1")]
    llm = _StubLLM(
        payload={
            "items": [
                {
                    "id": "s1",
                    "inputs": {
                        "user_business_value": ["not", "a", "number"],
                        "time_criticality": 1,
                        "risk_reduction_or_opportunity_enablement": 1,
                        "job_size": 3,
                    },
                    "rationale": "broken",
                }
            ]
        }
    )
    store = _fake_store(stories=stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="prod-x", method="wsjf", persist=True)
    assert len(result.ranked) == 1
    assert result.ranked[0].id == "s1"
    assert result.ranked[0].score == 0.0
    assert "malformed" in result.ranked[0].rationale.lower()
    # Synthetic row → no persistence (existing scores stay intact).
    assert store.persisted == []


def test_groom_only_sees_stories_under_requested_product() -> None:
    # Two products with disjoint stories. The agent must only score the
    # stories under the requested product — a regression to the old
    # `or True` predicate (or any other cross-product leak in the real
    # store's JOIN) would surface stories from the wrong product here.
    a_only = [_story("s-a")]
    b_only = [_story("s-b")]
    store = _FakeStore({"prod-a": a_only, "prod-b": b_only})

    inputs = {
        "user_business_value": 1,
        "time_criticality": 1,
        "risk_reduction_or_opportunity_enablement": 1,
        "job_size": 3,
    }
    llm = _StubLLM(
        payload={
            "items": [
                {"id": "s-a", "inputs": inputs, "rationale": ""},
                {"id": "s-b", "inputs": inputs, "rationale": ""},
            ]
        }
    )
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]

    result_a = agent.groom(product_id="prod-a", method="wsjf", persist=False)
    assert [r.id for r in result_a.ranked] == ["s-a"]

    result_b = agent.groom(product_id="prod-b", method="wsjf", persist=False)
    assert [r.id for r in result_b.ranked] == ["s-b"]

    # An unknown product id has no stories and the LLM is never called.
    pre_calls = len(llm.calls)
    result_missing = agent.groom(product_id="prod-missing", method="wsjf")
    assert result_missing.ranked == []
    assert len(llm.calls) == pre_calls


def test_groom_includes_stories_omitted_by_llm_with_score_zero() -> None:
    # Models truncate or skip uncertain items. The agent must still
    # surface every backlog story so downstream planning doesn't silently
    # lose work; the omitted ones are flagged for manual review and not
    # persisted (their existing scores stay intact).
    stories = [_story("scored", title="alpha"), _story("missed", title="beta")]
    llm = _StubLLM(
        payload={
            "items": [
                {
                    "id": "scored",
                    "inputs": {
                        "user_business_value": 5,
                        "time_criticality": 3,
                        "risk_reduction_or_opportunity_enablement": 1,
                        "job_size": 3,
                    },
                    "rationale": "covered",
                }
            ]
        }
    )
    store = _fake_store(stories=stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="prod-x", method="wsjf", persist=True)

    by_id = {r.id: r for r in result.ranked}
    assert set(by_id) == {"scored", "missed"}
    assert by_id["scored"].score == 3.0
    assert by_id["missed"].score == 0.0
    assert "manual review" in by_id["missed"].rationale
    # Persistence covers only the LLM-scored stories — never overwrites
    # the missed story's existing score with a synthetic zero.
    assert {r[0] for r in store.persisted} == {"scored"}


def test_groom_deduplicates_repeated_story_ids() -> None:
    # Repeated id in the LLM payload — agent must keep the first scoring
    # and ignore the rest so persisted scores stay deterministic.
    stories = [_story("s1")]
    llm = _StubLLM(
        payload={
            "items": [
                {
                    "id": "s1",
                    "inputs": {
                        "user_business_value": 8,
                        "time_criticality": 4,
                        "risk_reduction_or_opportunity_enablement": 0,
                        "job_size": 4,
                    },
                    "rationale": "first",
                },
                {
                    "id": "s1",
                    "inputs": {
                        "user_business_value": 1,
                        "time_criticality": 1,
                        "risk_reduction_or_opportunity_enablement": 0,
                        "job_size": 4,
                    },
                    "rationale": "second",
                },
            ]
        }
    )
    store = _fake_store(stories=stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="prod-x", method="wsjf", persist=True)

    assert len(result.ranked) == 1
    assert result.ranked[0].id == "s1"
    # First payload wins → cost_of_delay=12 / job_size=4 = 3.0
    assert result.ranked[0].score == 3.0
    assert result.ranked[0].rationale == "first"
    # Persistence wrote exactly one row.
    assert store.persisted == [("s1", 3.0, None)]
