"""Tests for ResultCache."""

import time

from deepthought.models import AgentResult
from deepthought.result_cache import ResultCache


def _result(answer="test answer"):
    return AgentResult(
        agent_id="a1",
        agent_name="test_agent",
        depth=0,
        focus_question="Q?",
        answer=answer,
        confidence=0.9,
    )


def test_put_and_get():
    cache = ResultCache()
    cache.put("What is X?", _result("X is Y"))
    hit = cache.get("What is X?")
    assert hit is not None
    assert hit.answer == "X is Y"


def test_case_insensitive_key():
    cache = ResultCache()
    cache.put("What is X?", _result())
    assert cache.get("what is x?") is not None
    assert cache.get("WHAT IS X?") is not None


def test_miss():
    cache = ResultCache()
    assert cache.get("nonexistent") is None


def test_ttl_expiration():
    cache = ResultCache(ttl=0.05)  # 50ms TTL
    cache.put("Q?", _result())
    assert cache.get("Q?") is not None
    time.sleep(0.1)
    assert cache.get("Q?") is None


def test_max_size_eviction():
    cache = ResultCache(max_size=3)
    for i in range(5):
        cache.put(f"question {i}", _result(f"answer {i}"))
    # Should not exceed max_size
    # The most recent entries should be present
    assert cache.get("question 4") is not None


def test_clear():
    cache = ResultCache()
    cache.put("Q?", _result())
    cache.clear()
    assert cache.get("Q?") is None
