"""Unit tests for ``social_media_marketing_team.shared.winning_posts_bank``.

Uses the dict-backed fake-Postgres pattern established by
``blogging/tests/test_story_bank.py``: a ``_FakeCursor`` routes the SQL
statements the module issues to an in-process store so save/find/
rerank/list/get/delete can be exercised without a live Postgres.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest


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

        if sql_l.startswith("insert into social_marketing_winning_posts"):
            (
                post_id,
                title,
                body,
                platform,
                keywords_json,
                metrics_json,
                engagement_score,
                linked_goals_json,
                summary,
                source_job_id,
                created_at,
            ) = params
            keywords = keywords_json.obj if hasattr(keywords_json, "obj") else keywords_json
            metrics = metrics_json.obj if hasattr(metrics_json, "obj") else metrics_json
            linked_goals = (
                linked_goals_json.obj if hasattr(linked_goals_json, "obj") else linked_goals_json
            )
            self._db["posts"][post_id] = {
                "id": post_id,
                "title": title,
                "body": body,
                "platform": platform or "",
                "keywords": list(keywords or []),
                "metrics": dict(metrics or {}),
                "engagement_score": float(engagement_score or 0.0),
                "linked_goals": list(linked_goals or []),
                "summary": summary or "",
                "source_job_id": source_job_id,
                "created_at": created_at,
            }
            self.rowcount = 1
            return

        if "from social_marketing_winning_posts where platform = any" in sql_l:
            (platforms,) = params
            allowed = set(platforms or [])
            self._last_fetch_all = [
                dict(row)
                for row in self._db["posts"].values()
                if (row["platform"] or "") in allowed
            ]
            return

        if "order by created_at desc limit" in sql_l:
            limit, offset = params
            ordered = sorted(
                self._db["posts"].values(),
                key=lambda r: r["created_at"],
                reverse=True,
            )
            self._last_fetch_all = [dict(row) for row in ordered[offset : offset + limit]]
            return

        if "where id = %s" in sql_l and sql_l.startswith("select"):
            (post_id,) = params
            row = self._db["posts"].get(post_id)
            self._last_fetch_one = dict(row) if row else None
            return

        if sql_l.startswith("select") and "from social_marketing_winning_posts" in sql_l:
            self._last_fetch_all = [dict(row) for row in self._db["posts"].values()]
            return

        if sql_l.startswith("delete from social_marketing_winning_posts where id"):
            (post_id,) = params
            if self._db["posts"].pop(post_id, None) is not None:
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
    """Install a fake ``get_conn`` on the bank module."""
    db: dict[str, Any] = {"posts": {}}

    @contextmanager
    def _fake_get_conn(database=None):
        yield _FakeConn(db)

    import social_media_marketing_team.shared.winning_posts_bank as wpb

    monkeypatch.setattr(wpb, "get_conn", _fake_get_conn)
    yield db


# ---------------------------------------------------------------------------
# save_winning_post
# ---------------------------------------------------------------------------


def test_save_persists_row_with_generated_id(fake_pg):
    from social_media_marketing_team.shared.winning_posts_bank import save_winning_post

    pid = save_winning_post(
        title="Why seed rounds fail",
        body="Founders often skip...",
        platform="linkedin",
        keywords=["founders", "seed"],
        metrics={"engagement_rate": 0.81},
        engagement_score=0.81,
        linked_goals=["awareness"],
        source_job_id="job-1",
    )
    assert isinstance(pid, str)
    assert len(pid) == 12
    row = fake_pg["posts"][pid]
    assert row["title"] == "Why seed rounds fail"
    assert row["platform"] == "linkedin"
    assert row["keywords"] == ["founders", "seed"]
    assert row["metrics"] == {"engagement_rate": 0.81}
    assert row["engagement_score"] == pytest.approx(0.81)
    assert row["linked_goals"] == ["awareness"]
    assert row["source_job_id"] == "job-1"
    assert row["summary"] == ""  # no llm client + no provided summary
    assert isinstance(row["created_at"], datetime)
    assert row["created_at"].tzinfo is timezone.utc


def test_save_defaults_empty_for_optional_fields(fake_pg):
    from social_media_marketing_team.shared.winning_posts_bank import save_winning_post

    pid = save_winning_post(title="t", body="b")
    row = fake_pg["posts"][pid]
    assert row["platform"] == ""
    assert row["keywords"] == []
    assert row["metrics"] == {}
    assert row["linked_goals"] == []
    assert row["source_job_id"] is None
    assert row["engagement_score"] == 0.0


def test_save_calls_llm_for_summary_when_client_provided(fake_pg):
    from social_media_marketing_team.shared.winning_posts_bank import save_winning_post

    class _StubLLM:
        def __init__(self):
            self.calls = []

        def complete(self, prompt: str, system_prompt: str = "") -> str:
            self.calls.append(prompt)
            return "  A concise summary.  "

    llm = _StubLLM()
    pid = save_winning_post(title="t", body="a long post body", llm_client=llm)
    assert fake_pg["posts"][pid]["summary"] == "A concise summary."
    assert len(llm.calls) == 1
    assert "Summarize this social post" in llm.calls[0]


def test_save_llm_failure_is_non_fatal(fake_pg, caplog):
    from social_media_marketing_team.shared.winning_posts_bank import save_winning_post

    class _ExplodingLLM:
        def complete(self, *a, **k):
            raise RuntimeError("llm is down")

    with caplog.at_level("WARNING"):
        pid = save_winning_post(title="t", body="b", llm_client=_ExplodingLLM())

    assert fake_pg["posts"][pid]["summary"] == ""
    assert any("summary generation failed" in rec.message for rec in caplog.records)


def test_save_uses_provided_summary_and_skips_llm(fake_pg):
    from social_media_marketing_team.shared.winning_posts_bank import save_winning_post

    class _LLM:
        def complete(self, *a, **k):
            raise AssertionError("LLM should not be called when summary is provided")

    pid = save_winning_post(title="t", body="b", summary="Already summarized.", llm_client=_LLM())
    assert fake_pg["posts"][pid]["summary"] == "Already summarized."


# ---------------------------------------------------------------------------
# find_relevant_winning_posts
# ---------------------------------------------------------------------------


def test_find_returns_empty_for_empty_query(fake_pg):
    from social_media_marketing_team.shared.winning_posts_bank import (
        find_relevant_winning_posts,
        save_winning_post,
    )

    save_winning_post(title="t", body="b", keywords=["founders"])
    assert find_relevant_winning_posts([]) == []


def test_find_scores_by_keyword_overlap(fake_pg):
    from social_media_marketing_team.shared.winning_posts_bank import (
        find_relevant_winning_posts,
        save_winning_post,
    )

    a = save_winning_post(title="A", body="", keywords=["founders", "seed", "growth"])
    b = save_winning_post(title="B", body="", keywords=["founders", "pricing"])
    c = save_winning_post(title="C", body="", keywords=["saas", "growth"])
    _d = save_winning_post(title="D", body="", keywords=["unrelated"])

    results = find_relevant_winning_posts(["founders", "growth"], limit=3)
    assert [r["id"] for r in results] == [a, b, c]


def test_find_ignores_case_and_whitespace(fake_pg):
    from social_media_marketing_team.shared.winning_posts_bank import (
        find_relevant_winning_posts,
        save_winning_post,
    )

    pid = save_winning_post(title="A", body="", keywords=["Founders", "  Growth  "])
    results = find_relevant_winning_posts(["FOUNDERS"], limit=5)
    assert [r["id"] for r in results] == [pid]


def test_find_applies_platform_filter(fake_pg):
    from social_media_marketing_team.shared.winning_posts_bank import (
        find_relevant_winning_posts,
        save_winning_post,
    )

    save_winning_post(title="LI", body="", platform="linkedin", keywords=["growth"])
    save_winning_post(title="X", body="", platform="x", keywords=["growth"])

    results = find_relevant_winning_posts(["growth"], platforms=["linkedin"])
    assert len(results) == 1
    assert results[0]["platform"] == "linkedin"


def test_find_applies_limit(fake_pg):
    from social_media_marketing_team.shared.winning_posts_bank import (
        find_relevant_winning_posts,
        save_winning_post,
    )

    for i in range(6):
        save_winning_post(title=f"t-{i}", body="", keywords=["growth"])
    results = find_relevant_winning_posts(["growth"], limit=3)
    assert len(results) == 3


def test_find_tiebreaks_by_engagement_score(fake_pg):
    from social_media_marketing_team.shared.winning_posts_bank import (
        find_relevant_winning_posts,
        save_winning_post,
    )

    low = save_winning_post(title="low", body="", keywords=["growth"], engagement_score=0.4)
    high = save_winning_post(title="high", body="", keywords=["growth"], engagement_score=0.9)

    results = find_relevant_winning_posts(["growth"], limit=2)
    assert results[0]["id"] == high
    assert results[1]["id"] == low


def test_find_llm_rerank_reorders_when_enough_candidates(fake_pg):
    from social_media_marketing_team.shared.winning_posts_bank import (
        find_relevant_winning_posts,
        save_winning_post,
    )

    class _SummaryLLM:
        def complete(self, *a, **k):
            return "summary"

        def complete_json(self, prompt: str, system_prompt: str = ""):
            return [3, 2, 1]

    llm = _SummaryLLM()
    a = save_winning_post(title="A", body="body", keywords=["growth"], llm_client=llm)
    b = save_winning_post(title="B", body="body", keywords=["growth"], llm_client=llm)
    c = save_winning_post(title="C", body="body", keywords=["growth"], llm_client=llm)

    results = find_relevant_winning_posts(
        ["growth"],
        limit=2,
        rerank_context="need winners for growth campaign",
        llm_client=llm,
    )
    assert len(results) == 2
    assert {r["id"] for r in results} <= {a, b, c}


def test_find_rerank_failure_falls_back_to_keyword(fake_pg):
    from social_media_marketing_team.shared.winning_posts_bank import (
        find_relevant_winning_posts,
        save_winning_post,
    )

    class _BrokenLLM:
        def complete(self, *a, **k):
            return "ok"

        def complete_json(self, *a, **k):
            raise RuntimeError("llm down")

    llm = _BrokenLLM()
    for _ in range(6):
        save_winning_post(title="t", body="b", keywords=["growth"], llm_client=llm)

    results = find_relevant_winning_posts(
        ["growth"],
        limit=2,
        rerank_context="ctx",
        llm_client=llm,
    )
    assert len(results) == 2


def test_find_rerank_disabled_via_env(fake_pg, monkeypatch):
    from social_media_marketing_team.shared.winning_posts_bank import (
        find_relevant_winning_posts,
        save_winning_post,
    )

    monkeypatch.setenv("SOCIAL_MARKETING_WINNING_POSTS_RERANK_ENABLED", "false")

    class _LoudLLM:
        def complete(self, *a, **k):
            return "summary"

        def complete_json(self, *a, **k):
            raise AssertionError("rerank must not run when disabled")

    llm = _LoudLLM()
    for _ in range(6):
        save_winning_post(title="t", body="b", keywords=["growth"], llm_client=llm)

    results = find_relevant_winning_posts(["growth"], limit=2, rerank_context="ctx", llm_client=llm)
    assert len(results) == 2  # keyword path only


# ---------------------------------------------------------------------------
# list / get / delete
# ---------------------------------------------------------------------------


def test_list_orders_newest_first(fake_pg, monkeypatch):
    import social_media_marketing_team.shared.winning_posts_bank as wpb

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    counter = {"i": 0}

    class _FakeDatetime:
        @staticmethod
        def now(tz=None):
            counter["i"] += 1
            return base + timedelta(minutes=counter["i"])

    monkeypatch.setattr(wpb, "datetime", _FakeDatetime)

    a = wpb.save_winning_post(title="oldest", body="")
    b = wpb.save_winning_post(title="middle", body="")
    c = wpb.save_winning_post(title="newest", body="")

    listing = wpb.list_winning_posts(limit=10)
    assert [r["id"] for r in listing] == [c, b, a]
    assert all(isinstance(r["created_at"], str) for r in listing)


def test_list_respects_limit_and_offset(fake_pg):
    from social_media_marketing_team.shared.winning_posts_bank import (
        list_winning_posts,
        save_winning_post,
    )

    for i in range(5):
        save_winning_post(title=f"t-{i}", body="")
    page1 = list_winning_posts(limit=2, offset=0)
    page2 = list_winning_posts(limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert {r["id"] for r in page1} & {r["id"] for r in page2} == set()


def test_get_returns_dict_for_existing(fake_pg):
    from social_media_marketing_team.shared.winning_posts_bank import (
        get_winning_post,
        save_winning_post,
    )

    pid = save_winning_post(title="t", body="b", keywords=["a", "b"])
    row = get_winning_post(pid)
    assert row is not None
    assert row["id"] == pid
    assert row["keywords"] == ["a", "b"]
    assert isinstance(row["created_at"], str)


def test_get_returns_none_for_missing(fake_pg):
    from social_media_marketing_team.shared.winning_posts_bank import get_winning_post

    assert get_winning_post("nope") is None


def test_delete_returns_true_on_hit(fake_pg):
    from social_media_marketing_team.shared.winning_posts_bank import (
        delete_winning_post,
        save_winning_post,
    )

    pid = save_winning_post(title="t", body="")
    assert delete_winning_post(pid) is True
    assert pid not in fake_pg["posts"]


def test_delete_returns_false_on_miss(fake_pg):
    from social_media_marketing_team.shared.winning_posts_bank import delete_winning_post

    assert delete_winning_post("never-existed") is False
