"""Unit tests for the review verdict cache serialize/deserialize helpers.

Covers ``serialize_review_cache`` and ``deserialize_review_cache`` in
``swarm_review.py``: normal operation, the 20-entry size cap, corruption
tolerance on deserialize, and the round-trip property the two functions
are meant to preserve.
"""

from __future__ import annotations

import copy
from typing import Any, Dict

import pytest

from software_engineering_team.swarm_review import (
    deserialize_review_cache,
    serialize_review_cache,
)


def _verdict(approved: bool = True, **extra: Any) -> Dict[str, Any]:
    verdict: Dict[str, Any] = {"approved": approved}
    verdict.update(extra)
    return verdict


def _cache(n: int) -> Dict[str, tuple]:
    return {
        f"task-{i}": (f"cache-key-{i}", _verdict(approved=i % 2 == 0, reason=f"r{i}"))
        for i in range(n)
    }


class TestSerializeReviewCache:
    def test_empty_cache_serializes_to_empty_list(self):
        assert serialize_review_cache({}) == []

    def test_five_entries_round_trip_shape(self):
        cache = _cache(5)
        serialized = serialize_review_cache(cache)
        assert len(serialized) == 5
        for entry, (task_id, (cache_key, verdict)) in zip(serialized, cache.items()):
            assert entry == {"task_id": task_id, "cache_key": cache_key, "verdict": verdict}

    def test_twenty_five_entries_caps_at_twenty_most_recent(self):
        cache = _cache(25)
        serialized = serialize_review_cache(cache)
        assert len(serialized) == 20
        expected_task_ids = [f"task-{i}" for i in range(5, 25)]
        assert [entry["task_id"] for entry in serialized] == expected_task_ids

    def test_verdict_is_deep_copied_not_aliased(self):
        cache = {"task-0": ("key-0", {"approved": True, "nested": {"a": 1}})}
        serialized = serialize_review_cache(cache)
        cache["task-0"][1]["nested"]["a"] = 999
        assert serialized[0]["verdict"]["nested"]["a"] == 1


class TestDeserializeReviewCache:
    def test_none_returns_empty_dict(self):
        assert deserialize_review_cache(None) == {}

    @pytest.mark.parametrize("bad_input", ["a string", 123, {"not": "a list"}, 1.5, True])
    def test_non_list_inputs_return_empty_dict(self, bad_input):
        assert deserialize_review_cache(bad_input) == {}

    def test_malformed_entry_missing_required_keys_is_skipped(self):
        assert deserialize_review_cache([{"bad": "data"}]) == {}

    def test_non_dict_entry_in_list_is_skipped(self):
        assert deserialize_review_cache(["not a dict", 123, None]) == {}

    @pytest.mark.parametrize(
        "entry",
        [
            {"cache_key": "k", "verdict": {"approved": True}},
            {"task_id": "t", "verdict": {"approved": True}},
            {"task_id": "t", "cache_key": "k"},
        ],
    )
    def test_entry_missing_one_required_key_is_skipped(self, entry):
        assert deserialize_review_cache([entry]) == {}

    def test_non_string_task_id_or_cache_key_is_skipped(self):
        assert (
            deserialize_review_cache(
                [{"task_id": 123, "cache_key": "k", "verdict": {"approved": True}}]
            )
            == {}
        )
        assert (
            deserialize_review_cache(
                [{"task_id": "t", "cache_key": 456, "verdict": {"approved": True}}]
            )
            == {}
        )

    def test_non_dict_verdict_is_skipped(self):
        assert (
            deserialize_review_cache([{"task_id": "t", "cache_key": "k", "verdict": "not a dict"}])
            == {}
        )

    def test_verdict_missing_approved_is_skipped(self):
        assert deserialize_review_cache([{"task_id": "t", "cache_key": "k", "verdict": {}}]) == {}

    def test_verdict_with_non_bool_approved_is_skipped(self):
        assert (
            deserialize_review_cache(
                [{"task_id": "t", "cache_key": "k", "verdict": {"approved": "false"}}]
            )
            == {}
        )

    def test_verdict_with_truthy_error_is_skipped(self):
        assert (
            deserialize_review_cache(
                [{"task_id": "t", "cache_key": "k", "verdict": {"approved": True, "error": "boom"}}]
            )
            == {}
        )

    def test_verdict_with_falsy_error_is_kept(self):
        result = deserialize_review_cache(
            [{"task_id": "t", "cache_key": "k", "verdict": {"approved": True, "error": ""}}]
        )
        assert "t" in result

    def test_verdict_with_non_string_reason_is_skipped(self):
        assert (
            deserialize_review_cache(
                [{"task_id": "t", "cache_key": "k", "verdict": {"approved": True, "reason": 123}}]
            )
            == {}
        )

    def test_verdict_with_non_list_requested_changes_is_skipped(self):
        assert (
            deserialize_review_cache(
                [
                    {
                        "task_id": "t",
                        "cache_key": "k",
                        "verdict": {"approved": True, "requested_changes": "not a list"},
                    }
                ]
            )
            == {}
        )

    def test_mixed_valid_and_invalid_entries(self):
        data = [
            {"task_id": "good-1", "cache_key": "k1", "verdict": {"approved": True}},
            {"bad": "data"},
            {
                "task_id": "good-2",
                "cache_key": "k2",
                "verdict": {"approved": False, "reason": "no"},
            },
            {"task_id": "bad-verdict", "cache_key": "k3", "verdict": "nope"},
            {"task_id": 42, "cache_key": "k4", "verdict": {"approved": True}},
        ]
        result = deserialize_review_cache(data)
        assert set(result.keys()) == {"good-1", "good-2"}
        assert result["good-1"] == ("k1", {"approved": True})
        assert result["good-2"] == ("k2", {"approved": False, "reason": "no"})

    def test_duplicate_task_id_later_entry_wins(self):
        data = [
            {"task_id": "t", "cache_key": "k1", "verdict": {"approved": True}},
            {"task_id": "t", "cache_key": "k2", "verdict": {"approved": False}},
        ]
        result = deserialize_review_cache(data)
        assert result == {"t": ("k2", {"approved": False})}

    def test_nested_verdict_structures_round_trip(self):
        verdict = {
            "approved": True,
            "requested_changes": ["fix a", "fix b"],
            "details": {"scores": [1, 2, 3], "meta": {"reviewer": "tech-lead"}},
        }
        data = [{"task_id": "t", "cache_key": "k", "verdict": verdict}]
        result = deserialize_review_cache(data)
        assert result == {"t": ("k", verdict)}


class TestRoundTrip:
    def test_round_trip_five_entries(self):
        cache = _cache(5)
        assert deserialize_review_cache(serialize_review_cache(cache)) == cache

    def test_round_trip_exactly_twenty_entries(self):
        cache = {}
        for i in range(20):
            verdict: Dict[str, Any] = {"approved": i % 3 == 0}
            if i % 2 == 0:
                verdict["reason"] = f"reason-{i}"
            if i % 4 == 0:
                verdict["requested_changes"] = [f"change-{i}-a", f"change-{i}-b"]
            verdict["nested"] = {"scores": [i, i + 1], "meta": {"idx": i}}
            cache[f"task-{i}"] = (f"key-{i}", verdict)

        original = copy.deepcopy(cache)
        result = deserialize_review_cache(serialize_review_cache(cache))
        assert result == original
