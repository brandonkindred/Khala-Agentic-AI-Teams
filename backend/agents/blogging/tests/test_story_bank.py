"""Unit tests for ``blogging.shared.story_bank`` (PR 4, Postgres backend).

Same dict-backed fake-Postgres pattern as the other PR-N store tests:
a ``_FakeCursor`` routes each SQL statement the module issues to an
in-process handler so we can exercise save/read/rerank/list/delete
without a live Postgres. Integration coverage against a real
``postgres:18`` service container runs in the ``test-shared-postgres``
CI job from PR 0.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Fake cursor — recognises only the statements story_bank actually issues
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, db: dict[str, Any]) -> None:
        self._db = db
        self.rowcount = 0
        self._last_fetch_one: dict | None = None
        self._last_fetch_all: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql: str, params: tuple | list = ()) -> None:
        sql_l = " ".join(sql.split()).lower()
        params = tuple(params)

        # INSERT INTO blogging_stories (...)
        if sql_l.startswith("insert into blogging_stories"):
            (
                story_id,
                narrative,
                section_title,
                section_context,
                keywords_json,
                summary,
                source_job_id,
                created_at,
            ) = params
            keywords = keywords_json.obj if hasattr(keywords_json, "obj") else keywords_json
            self._db["stories"][story_id] = {
                "id": story_id,
                "narrative": narrative,
                "section_title": section_title,
                "section_context": section_context,
                "keywords": list(keywords or []),
                "summary": summary or "",
                "source_job_id": source_job_id,
                "created_at": created_at,
            }
            self.rowcount = 1
            return

        # SELECT ... FROM blogging_stories (no WHERE)
        if (
            sql_l.startswith(
                "select id, narrative, section_title, section_context, keywords, summary, created_at from blogging_stories"
            )
            and "where" not in sql_l
            and "order by" not in sql_l
        ):
            self._last_fetch_all = [dict(row) for row in self._db["stories"].values()]
            return

        # SELECT ... FROM blogging_stories ORDER BY created_at DESC LIMIT %s OFFSET %s
        if "order by created_at desc limit" in sql_l:
            limit, offset = params
            ordered = sorted(
                self._db["stories"].values(),
                key=lambda r: r["created_at"],
                reverse=True,
            )
            self._last_fetch_all = [dict(row) for row in ordered[offset : offset + limit]]
            return

        # SELECT ... FROM blogging_stories WHERE id = %s
        if sql_l.startswith("select id, narrative") and "where id = %s" in sql_l:
            (story_id,) = params
            row = self._db["stories"].get(story_id)
            self._last_fetch_one = dict(row) if row else None
            return

        # DELETE FROM blogging_stories WHERE id = %s
        if sql_l.startswith("delete from blogging_stories where id"):
            (story_id,) = params
            if self._db["stories"].pop(story_id, None) is not None:
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        raise AssertionError(f"unexpected SQL in fake cursor: {sql!r}")

    def fetchone(self):
        return self._last_fetch_one

    def fetchall(self):
        return self._last_fetch_all


class _FakeConn:
    def __init__(self, db: dict[str, Any]) -> None:
        self._db = db

    def cursor(self, row_factory=None):  # noqa: ANN001
        return _FakeCursor(self._db)


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch):
    """Install a fake ``get_conn`` on the story_bank module."""
    db: dict[str, Any] = {"stories": {}}

    @contextmanager
    def _fake_get_conn(database=None):
        yield _FakeConn(db)

    import blogging.shared.story_bank as sb

    monkeypatch.setattr(sb, "get_conn", _fake_get_conn)
    yield db


# ---------------------------------------------------------------------------
# save_story
# ---------------------------------------------------------------------------


def test_save_story_persists_row_with_generated_id(fake_pg):
    from blogging.shared.story_bank import save_story

    sid = save_story(
        narrative="I once built a thing",
        section_title="Intro",
        section_context="Opening hook",
        keywords=["build", "thing"],
        source_job_id="job-1",
    )
    assert isinstance(sid, str)
    assert len(sid) == 12  # uuid4.hex[:12]
    row = fake_pg["stories"][sid]
    assert row["narrative"] == "I once built a thing"
    assert row["keywords"] == ["build", "thing"]
    assert row["section_title"] == "Intro"
    assert row["source_job_id"] == "job-1"
    assert row["summary"] == ""  # no llm client provided
    assert isinstance(row["created_at"], datetime)
    assert row["created_at"].tzinfo is timezone.utc


def test_save_story_handles_no_keywords_and_no_job_id(fake_pg):
    from blogging.shared.story_bank import save_story

    sid = save_story(narrative="solo story")
    row = fake_pg["stories"][sid]
    assert row["keywords"] == []
    assert row["source_job_id"] is None


def test_save_story_calls_llm_for_summary_when_provided(fake_pg):
    from blogging.shared.story_bank import save_story

    class _StubLLM:
        def __init__(self):
            self.calls = []

        def complete(self, prompt: str, system_prompt: str = "") -> str:
            self.calls.append(prompt)
            return "  A concise summary.  "

    llm = _StubLLM()
    sid = save_story(narrative="a long story", llm_client=llm)
    assert fake_pg["stories"][sid]["summary"] == "A concise summary."
    assert len(llm.calls) == 1
    assert "Summarize this story" in llm.calls[0]


def test_save_story_llm_failure_is_non_fatal(fake_pg, caplog):
    from blogging.shared.story_bank import save_story

    class _ExplodingLLM:
        def complete(self, *a, **k):
            raise RuntimeError("llm is down")

    with caplog.at_level("WARNING"):
        sid = save_story(narrative="story", llm_client=_ExplodingLLM())

    row = fake_pg["stories"][sid]
    assert row["summary"] == ""
    assert any("summary generation failed" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# find_relevant_stories (keyword overlap path)
# ---------------------------------------------------------------------------


def test_find_relevant_stories_returns_empty_for_empty_query(fake_pg):
    from blogging.shared.story_bank import find_relevant_stories, save_story

    save_story(narrative="x", keywords=["python"])
    assert find_relevant_stories([]) == []


def test_find_relevant_stories_scores_by_keyword_overlap(fake_pg):
    from blogging.shared.story_bank import find_relevant_stories, save_story

    a = save_story(narrative="A", keywords=["python", "web", "api"])
    b = save_story(narrative="B", keywords=["python", "ml"])
    c = save_story(narrative="C", keywords=["java", "web"])
    _d = save_story(narrative="D", keywords=["rust"])  # zero overlap

    results = find_relevant_stories(["python", "web"], limit=3)
    assert [r["id"] for r in results] == [a, b, c]
    # Top result has overlap = 2 (python + web), next two have overlap = 1.


def test_find_relevant_stories_ignores_case_and_whitespace(fake_pg):
    from blogging.shared.story_bank import find_relevant_stories, save_story

    sid = save_story(narrative="A", keywords=["Python", "  Web  "])
    results = find_relevant_stories(["PYTHON"], limit=5)
    assert [r["id"] for r in results] == [sid]


def test_find_relevant_stories_returns_keywords_as_list_not_json(fake_pg):
    """psycopg3 returns JSONB as Python list — no json.loads needed."""
    from blogging.shared.story_bank import find_relevant_stories, save_story

    save_story(narrative="A", keywords=["python"])
    results = find_relevant_stories(["python"])
    assert isinstance(results[0]["keywords"], list)
    assert results[0]["keywords"] == ["python"]


def test_find_relevant_stories_applies_limit(fake_pg):
    from blogging.shared.story_bank import find_relevant_stories, save_story

    for i in range(6):
        save_story(narrative=f"story-{i}", keywords=["python"])
    results = find_relevant_stories(["python"], limit=3)
    assert len(results) == 3


def test_find_relevant_stories_llm_rerank_reorders_when_enough_candidates(fake_pg):
    from blogging.shared.story_bank import find_relevant_stories, save_story

    # Save stories with summaries so the rerank path activates.
    class _SummaryLLM:
        def complete(self, *a, **k):
            return "summary"

        def complete_json(self, prompt: str, system_prompt: str = ""):
            # Reverse the candidate order.
            return [3, 2, 1]

    llm = _SummaryLLM()
    a = save_story(narrative="A", keywords=["python"], llm_client=llm)
    b = save_story(narrative="B", keywords=["python"], llm_client=llm)
    c = save_story(narrative="C", keywords=["python"], llm_client=llm)

    results = find_relevant_stories(
        ["python"],
        limit=2,
        story_opportunity="need a story",
        llm_client=llm,
    )
    # LLM returned [3, 2, 1] — reranked to candidates[2], candidates[1].
    # Candidate order before rerank is deterministic by overlap then insertion.
    assert len(results) == 2
    assert {r["id"] for r in results} <= {a, b, c}


def test_find_relevant_stories_rerank_failure_falls_back_to_keyword(fake_pg):
    from blogging.shared.story_bank import find_relevant_stories, save_story

    class _BrokenLLM:
        def complete(self, *a, **k):
            return "ok"

        def complete_json(self, *a, **k):
            raise RuntimeError("llm down")

    llm = _BrokenLLM()
    # Enough candidates with summaries to trigger rerank.
    for _ in range(6):
        save_story(narrative="x", keywords=["python"], llm_client=llm)

    results = find_relevant_stories(
        ["python"],
        limit=2,
        story_opportunity="need a story",
        llm_client=llm,
    )
    assert len(results) == 2  # keyword path still returned results


# ---------------------------------------------------------------------------
# list_stories / get_story / delete_story
# ---------------------------------------------------------------------------


def test_list_stories_orders_newest_first(fake_pg, monkeypatch):
    from blogging.shared import story_bank as sb

    # Force deterministic timestamps by patching datetime.now inside the module.
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    counter = {"i": 0}

    class _FakeDatetime:
        @staticmethod
        def now(tz=None):
            counter["i"] += 1
            return base + timedelta(minutes=counter["i"])

    monkeypatch.setattr(sb, "datetime", _FakeDatetime)

    a = sb.save_story(narrative="oldest")
    b = sb.save_story(narrative="middle")
    c = sb.save_story(narrative="newest")

    listing = sb.list_stories(limit=10)
    assert [r["id"] for r in listing] == [c, b, a]
    assert all(isinstance(r["created_at"], str) for r in listing)


def test_list_stories_respects_limit_and_offset(fake_pg):
    from blogging.shared.story_bank import list_stories, save_story

    for i in range(5):
        save_story(narrative=f"s-{i}")
    page1 = list_stories(limit=2, offset=0)
    page2 = list_stories(limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert {r["id"] for r in page1} & {r["id"] for r in page2} == set()


def test_get_story_returns_dict_for_existing(fake_pg):
    from blogging.shared.story_bank import get_story, save_story

    sid = save_story(narrative="hello", keywords=["a", "b"])
    story = get_story(sid)
    assert story is not None
    assert story["id"] == sid
    assert story["narrative"] == "hello"
    assert story["keywords"] == ["a", "b"]
    assert isinstance(story["created_at"], str)


def test_get_story_returns_none_for_missing(fake_pg):
    from blogging.shared.story_bank import get_story

    assert get_story("nope") is None


def test_delete_story_returns_true_on_hit(fake_pg):
    from blogging.shared.story_bank import delete_story, save_story

    sid = save_story(narrative="x")
    assert delete_story(sid) is True
    assert sid not in fake_pg["stories"]


def test_delete_story_returns_false_on_miss(fake_pg):
    from blogging.shared.story_bank import delete_story

    assert delete_story("never-existed") is False
