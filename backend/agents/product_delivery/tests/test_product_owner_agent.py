"""Unit tests for :class:`ProductOwnerAgent` with a stub LLM client."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

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


def test_groom_surfaces_non_dict_inputs_with_score_zero_no_persist() -> None:
    # When the LLM returns `inputs: null` (or a list/string), the
    # agent must NOT silently substitute `{}` and persist a synthetic
    # zero — that would overwrite the row's existing real score.
    # Route through the malformed fallback (visible-only, no persist).
    stories = [_story("s1")]
    llm = _StubLLM(
        payload={
            "items": [
                {
                    "id": "s1",
                    "inputs": None,  # non-dict — must trigger fallback
                    "rationale": "model gave up",
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
    # Critical: synthetic row must NOT be persisted.
    assert store.persisted == []


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


def test_groom_rationale_uses_actual_persisted_count() -> None:
    # If `bulk_update_story_scores` reports fewer rows updated than we
    # tried to score (e.g. concurrent delete), the rationale must
    # reflect that — it's the signal downstream automation parses to
    # measure grooming success.
    stories = [_story("s1"), _story("s2")]
    inputs = {
        "user_business_value": 1,
        "time_criticality": 1,
        "risk_reduction_or_opportunity_enablement": 1,
        "job_size": 3,
    }
    llm = _StubLLM(
        payload={
            "items": [
                {"id": "s1", "inputs": inputs, "rationale": "ok"},
                {"id": "s2", "inputs": inputs, "rationale": "ok"},
            ]
        }
    )
    store = _fake_store(stories=stories)
    # Stub bulk_update_story_scores to claim only one row landed (the
    # other was deleted between ranking and persistence).
    store.bulk_update_story_scores = lambda rows: 1  # type: ignore[assignment]
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="prod-x", method="wsjf", persist=True)
    assert "Scored 1/2" in result.rationale
    assert "manual review" in result.rationale.lower()


def test_groom_treats_empty_inputs_dict_as_malformed_no_persist() -> None:
    # Codex flagged: an empty `{}` for `inputs` was scored to 0 and
    # persisted, overwriting any existing real score on the row. Now
    # the agent treats empty-dict same as None — fallback row visible
    # but never persisted.
    stories = [_story("s1")]
    llm = _StubLLM(payload={"items": [{"id": "s1", "inputs": {}, "rationale": "x"}]})
    store = _fake_store(stories=stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="prod-x", method="wsjf", persist=True)
    assert len(result.ranked) == 1
    assert result.ranked[0].score == 0.0
    assert "malformed" in result.ranked[0].rationale.lower()
    assert store.persisted == []


def test_groom_handles_overflow_error_in_inputs() -> None:
    # `_to_float` can raise `OverflowError` for huge JSON integers
    # (e.g. an LLM emitting `reach: 10**400`). The agent must still
    # surface the story via the malformed fallback rather than 500.
    stories = [_story("s1")]
    llm = _StubLLM(
        payload={
            "items": [
                {
                    "id": "s1",
                    "inputs": {
                        "reach": 10**400,  # int well outside float64
                        "impact": 1,
                        "confidence": 1,
                        "effort": 1,
                    },
                    "rationale": "huge",
                }
            ]
        }
    )
    store = _fake_store(stories=stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="prod-x", method="rice", persist=True)
    assert len(result.ranked) == 1
    assert result.ranked[0].score == 0.0
    assert "malformed" in result.ranked[0].rationale.lower()
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


@pytest.mark.parametrize("bad_value", [True, False])
def test_groom_rejects_boolean_llm_inputs_as_malformed(bad_value: bool) -> None:
    """`_to_float` must reject JSON booleans before they silently become 1.0/0.0.

    Without this guard a confused LLM emitting booleans for any of the
    WSJF/RICE inputs would skew rankings (every story scored as
    ``cost_of_delay=1`` or ``=0``); we want those stories surfaced as
    malformed so a human can re-rank them, not persisted.
    """
    stories = [_story("s1")]
    llm = _StubLLM(
        payload={
            "items": [
                {
                    "id": "s1",
                    "inputs": {
                        "user_business_value": bad_value,
                        "time_criticality": 4,
                        "risk_reduction_or_opportunity_enablement": 0,
                        "job_size": 4,
                    },
                }
            ]
        }
    )
    store = _fake_store(stories=stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="prod-x", method="wsjf", persist=True)

    assert len(result.ranked) == 1
    assert result.ranked[0].score == 0.0
    assert "malformed" in result.ranked[0].rationale.lower()
    # Synthetic 0.0 must not be persisted — the existing score on the
    # row stays intact.
    assert store.persisted == []


def test_groom_caps_prompt_size_and_defers_overflow_stories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backlogs over the cap must defer the tail rather than 503 the whole run.

    The first ``cap`` stories are scored; the rest surface as score=0
    with a "deferred" rationale and are never sent to the LLM. The LLM
    must see exactly the capped slice (so prompt size is bounded).
    """
    monkeypatch.setenv("PRODUCT_DELIVERY_GROOM_MAX_STORIES", "2")
    stories = [_story(f"s{i}") for i in range(5)]
    llm = _StubLLM(
        payload={
            "items": [
                {
                    "id": "s0",
                    "inputs": {
                        "user_business_value": 8,
                        "time_criticality": 4,
                        "risk_reduction_or_opportunity_enablement": 0,
                        "job_size": 4,
                    },
                },
                {
                    "id": "s1",
                    "inputs": {
                        "user_business_value": 4,
                        "time_criticality": 2,
                        "risk_reduction_or_opportunity_enablement": 0,
                        "job_size": 4,
                    },
                },
            ]
        }
    )
    store = _fake_store(stories=stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="prod-x", method="wsjf", persist=True)

    # All five stories surface in the result so deferred work isn't
    # invisible to the planner.
    assert len(result.ranked) == 5
    by_id = {item.id: item for item in result.ranked}

    # The two scored stories carry real scores + were persisted.
    assert by_id["s0"].score == 3.0
    assert by_id["s1"].score == 1.5
    assert {r[0] for r in store.persisted} == {"s0", "s1"}

    # The deferred tail surfaces with score=0 + a "deferred" rationale
    # and was never persisted.
    for sid in ("s2", "s3", "s4"):
        item = by_id[sid]
        assert item.score == 0.0
        assert "deferred" in item.rationale.lower()

    # And, crucially, the LLM only saw the capped slice.
    assert len(llm.calls) == 1
    prompt = llm.calls[0]["prompt"]
    assert '"id": "s0"' in prompt
    assert '"id": "s1"' in prompt
    for omitted in ("s2", "s3", "s4"):
        assert f'"id": "{omitted}"' not in prompt

    # And the rationale calls out the deferral so an operator notices.
    assert "deferred" in result.rationale.lower()


@pytest.mark.parametrize(
    "bad_envelope",
    [
        # Codex flagged: a 200 with all stories zero-scored masks
        # provider/schema regressions as normal output. These shapes
        # must trip `LLMScoringUnavailable` so the route returns 503
        # and callers retry.
        {"items": "not a list"},
        {"items": None},
        {"results": [{"id": "s1", "inputs": {}}]},  # wrong key
        "definitely not a dict",
        ["lists are not envelopes"],
        42,
    ],
)
def test_groom_raises_503_on_malformed_llm_envelope(bad_envelope: Any) -> None:
    from product_delivery.product_owner_agent.agent import LLMScoringUnavailable

    stories = [_story("s1")]
    llm = _StubLLM(payload=bad_envelope)  # type: ignore[arg-type]
    store = _fake_store(stories=stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    with pytest.raises(LLMScoringUnavailable):
        agent.groom(product_id="prod-x", method="wsjf", persist=True)
    # No persistence on failure — the existing scores stay intact.
    assert store.persisted == []


def test_groom_grooming_window_rotates_by_updated_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cap selection must prefer least-recently-updated stories.

    If grooming always took ``stories[:cap]`` (creation order) every
    run, newer stories would be permanently starved in sustained large
    backlogs. The rotation by ``updated_at`` means scored stories'
    ``updated_at`` jumps to "now" on persist and they cycle to the
    end of the next run's window.
    """
    from datetime import timedelta

    monkeypatch.setenv("PRODUCT_DELIVERY_GROOM_MAX_STORIES", "1")
    base = datetime.now(tz=timezone.utc)

    # Two stories — `recent` was just touched, `stale` hasn't been
    # updated in a week. The rotation must pick `stale` for grooming
    # this run, not `recent` (which would be the first by id).
    recent = _story("recent")
    stale = _story("stale")
    object.__setattr__(recent, "updated_at", base)
    object.__setattr__(stale, "updated_at", base - timedelta(days=7))

    llm = _StubLLM(
        payload={
            "items": [
                {
                    "id": "stale",
                    "inputs": {
                        "user_business_value": 8,
                        "time_criticality": 4,
                        "risk_reduction_or_opportunity_enablement": 0,
                        "job_size": 4,
                    },
                }
            ]
        }
    )
    store = _fake_store(stories=[recent, stale])  # creation-order: recent first
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="prod-x", method="wsjf", persist=True)

    assert len(result.ranked) == 2
    # Stale was scored, recent was deferred — opposite of creation order.
    by_id = {item.id: item for item in result.ranked}
    assert by_id["stale"].score == 3.0
    assert by_id["recent"].score == 0.0
    assert "deferred" in by_id["recent"].rationale.lower()

    # And the LLM only saw the stale story.
    assert len(llm.calls) == 1
    prompt = llm.calls[0]["prompt"]
    assert '"id": "stale"' in prompt
    assert '"id": "recent"' not in prompt

    # Persistence covers only the scored story.
    assert {r[0] for r in store.persisted} == {"stale"}


def test_groom_trims_by_byte_budget_when_user_stories_are_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story-count cap alone doesn't protect against verbose ``user_story`` fields.

    A small backlog with multi-KB ``user_story`` text can still overrun
    model context. Set a small byte budget and verify that stories
    beyond what fits get deferred (even when count < cap), and the
    LLM only sees the trimmed slice.
    """
    monkeypatch.setenv("PRODUCT_DELIVERY_GROOM_MAX_STORIES", "100")  # well above story count
    monkeypatch.setenv("PRODUCT_DELIVERY_GROOM_MAX_PROMPT_BYTES", "1024")  # tight budget

    # Three stories with ~600-char `user_story` each, so two fit and
    # one overflows the 1 KiB budget.
    big_text = "x" * 600
    stories = []
    for sid in ("s1", "s2", "s3"):
        story = _story(sid)
        object.__setattr__(story, "user_story", big_text)
        stories.append(story)

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
                },
            ]
        }
    )
    store = _fake_store(stories=stories)
    agent = ProductOwnerAgent(store=store, llm_client=llm)  # type: ignore[arg-type]
    result = agent.groom(product_id="prod-x", method="wsjf", persist=True)

    # All three surface in result so no work disappears.
    assert len(result.ranked) == 3
    by_id = {item.id: item for item in result.ranked}

    # First story scored, the other two deferred for byte-budget reasons
    # (the second wouldn't fit either since 2 * 600 > 1024 budget).
    assert by_id["s1"].score == 3.0
    for sid in ("s2", "s3"):
        assert by_id[sid].score == 0.0
        assert "deferred" in by_id[sid].rationale.lower()

    # LLM only saw the slice that fit in the byte budget.
    prompt = llm.calls[0]["prompt"]
    assert '"id": "s1"' in prompt
    assert '"id": "s2"' not in prompt
    assert '"id": "s3"' not in prompt
