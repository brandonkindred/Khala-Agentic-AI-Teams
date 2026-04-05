"""Tests for SharedKnowledgeBase."""

from deepthought.knowledge_base import SharedKnowledgeBase
from deepthought.models import KnowledgeEntry


def _entry(name="agent_1", question="What is X?", finding="X is Y", conf=0.8, tags=None):
    return KnowledgeEntry(
        agent_id="id-1",
        agent_name=name,
        focus_question=question,
        finding=finding,
        confidence=conf,
        tags=tags or [],
    )


def test_add_and_retrieve():
    kb = SharedKnowledgeBase()
    kb.add(_entry())
    assert len(kb.all_entries()) == 1


def test_find_similar_exact():
    kb = SharedKnowledgeBase()
    kb.add(_entry(question="What is the meaning of life?"))
    results = kb.find_similar("What is the meaning of life?")
    assert len(results) == 1


def test_find_similar_close_match():
    kb = SharedKnowledgeBase()
    kb.add(_entry(question="What is the economic impact of climate change?"))
    results = kb.find_similar("What is the economic effect of climate change?")
    assert len(results) == 1


def test_find_similar_no_match():
    kb = SharedKnowledgeBase()
    kb.add(_entry(question="What is quantum entanglement?"))
    results = kb.find_similar("How do I bake a cake?")
    assert len(results) == 0


def test_find_by_tags():
    kb = SharedKnowledgeBase()
    kb.add(_entry(tags=["physics", "quantum"]))
    kb.add(_entry(name="agent_2", tags=["biology"]))
    results = kb.find_by_tags(["physics"])
    assert len(results) == 1
    assert results[0].agent_name == "agent_1"


def test_summary_for_prompt_empty():
    kb = SharedKnowledgeBase()
    assert "No prior findings" in kb.summary_for_prompt()


def test_summary_for_prompt_truncation():
    kb = SharedKnowledgeBase()
    for i in range(100):
        kb.add(_entry(name=f"agent_{i}", finding="X" * 200))
    summary = kb.summary_for_prompt(max_chars=500)
    assert len(summary) <= 600  # some slack for the truncation message
    assert "truncated" in summary


def test_thread_safety():
    """Multiple threads can write concurrently without corruption."""
    import threading

    kb = SharedKnowledgeBase()
    errors = []

    def writer(idx):
        try:
            for j in range(50):
                kb.add(_entry(name=f"thread_{idx}_{j}"))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(kb.all_entries()) == 250
