"""Tests for the process-global LLM concurrency gate (llm_service.concurrency)."""

import pytest

from llm_service.concurrency import get_llm_semaphore, reset_llm_semaphore


@pytest.fixture(autouse=True)
def _fresh_semaphore(monkeypatch: pytest.MonkeyPatch):
    """Each test starts from a clean gate and restores it afterward, since the
    semaphore is a process-global singleton frozen at first use."""
    monkeypatch.delenv("LLM_MAX_CONCURRENCY", raising=False)
    reset_llm_semaphore()
    yield
    reset_llm_semaphore()


def test_get_llm_semaphore_is_singleton() -> None:
    assert get_llm_semaphore() is get_llm_semaphore()


def test_limit_from_env_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "2")
    reset_llm_semaphore()
    sem = get_llm_semaphore()
    assert sem.acquire(blocking=False) is True
    assert sem.acquire(blocking=False) is True
    # The 3rd concurrent acquisition must fail — the gate admits exactly 2.
    assert sem.acquire(blocking=False) is False
    sem.release()
    sem.release()


def test_garbage_limit_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "not-an-int")
    reset_llm_semaphore()
    sem = get_llm_semaphore()
    acquired = [sem.acquire(blocking=False) for _ in range(4)]
    assert all(acquired)  # default is 4
    assert sem.acquire(blocking=False) is False
    for _ in range(4):
        sem.release()


@pytest.mark.parametrize("raw", ["0", "-1"])
def test_nonpositive_limit_floored_to_one(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", raw)
    reset_llm_semaphore()
    sem = get_llm_semaphore()
    assert sem.acquire(blocking=False) is True
    assert sem.acquire(blocking=False) is False  # floored to exactly 1
    sem.release()


def test_ollama_and_claude_share_one_gate() -> None:
    """Both provider clients resolve the same accessor, so the cap is one global
    limit across providers, not a separate cap per provider."""
    from llm_service.clients import claude, ollama

    assert claude.get_llm_semaphore is ollama.get_llm_semaphore
    assert claude.get_llm_semaphore() is ollama.get_llm_semaphore()
