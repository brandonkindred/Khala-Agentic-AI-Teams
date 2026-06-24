"""Unit tests for learning ingestion: post-mortems, gate outcomes, Tech Lead block."""

from __future__ import annotations

from types import SimpleNamespace

from software_engineering_team.shared import gate_outcomes, learnings_store, post_mortem_ingest
from software_engineering_team.shared.learnings_store import Learning
from software_engineering_team.tech_lead_agent.agent import TechLeadAgent, _learnings_top_n

# --- post_mortem_ingest ----------------------------------------------------


def test_learning_from_failure_builds_pattern(monkeypatch) -> None:
    """learning_from_failure builds a post-mortem-sourced learning from a recovery failure."""
    captured: dict = {}
    monkeypatch.setattr(
        learnings_store, "upsert_learning", lambda **kw: captured.update(kw) or True
    )
    ok = post_mortem_ingest.learning_from_failure(
        "backend_dev", "build the API", ValueError("boom")
    )
    assert ok is True
    assert captured["pattern"] == "Recovery failure in backend_dev"
    assert "boom" in captured["trigger"]
    assert captured["source"] == "post_mortem"


def test_learning_from_failure_requires_agent() -> None:
    """learning_from_failure returns False when the agent name is empty."""
    assert post_mortem_ingest.learning_from_failure("", "task", "err") is False


def test_ingest_post_mortems_file_parses_entries(tmp_path, monkeypatch) -> None:
    """ingest_post_mortems_file parses each failure entry and ingests a learning per agent."""
    calls: list = []
    monkeypatch.setattr(
        post_mortem_ingest,
        "learning_from_failure",
        lambda agent, desc, err: calls.append((agent, err)) or True,
    )
    md = tmp_path / "POST_MORTEMS.md"
    md.write_text(
        "# Log\n\n---\n\n"
        "## Failure: 2026-01-01 12:00:00 - backend_dev\n\n"
        "### What Went Wrong\n\n- **Final error**: `KeyError: 'x'`\n\n---\n\n"
        "## Failure: 2026-01-02 09:00:00 - frontend_dev\n\n"
        "### What Went Wrong\n\n- **Final error**: `TypeError: bad`\n\n---\n",
        encoding="utf-8",
    )
    n = post_mortem_ingest.ingest_post_mortems_file(md)
    assert n == 2
    assert calls[0][0] == "backend_dev"
    assert "KeyError" in calls[0][1]
    assert calls[1][0] == "frontend_dev"


def test_ingest_missing_file_is_zero() -> None:
    """ingest_post_mortems_file returns 0 for a missing file."""
    assert post_mortem_ingest.ingest_post_mortems_file("/no/such/file.md") == 0


def test_ingest_file_without_failure_entries_is_zero(tmp_path) -> None:
    """A file that exists but has no '## Failure:' entries ingests nothing (returns 0)."""
    p = tmp_path / "POST_MORTEMS.md"
    p.write_text("# Post-mortems\n\nNo failures recorded yet.\n", encoding="utf-8")
    assert post_mortem_ingest.ingest_post_mortems_file(p) == 0


# --- gate_outcomes ---------------------------------------------------------


def test_is_rejected_variants() -> None:
    """is_rejected reads approved/all_satisfied flags and returns None when neither is present."""
    assert gate_outcomes.is_rejected(SimpleNamespace(approved=False)) is True
    assert gate_outcomes.is_rejected(SimpleNamespace(approved=True)) is False
    assert gate_outcomes.is_rejected(SimpleNamespace(all_satisfied=False)) is True
    assert gate_outcomes.is_rejected(SimpleNamespace(all_satisfied=True)) is False
    assert gate_outcomes.is_rejected(SimpleNamespace()) is None


def test_record_gate_outcome_on_pass_is_noop(monkeypatch) -> None:
    """record_gate_outcome is a no-op (returns False) when the gate passed."""
    monkeypatch.setattr(gate_outcomes, "_first_issue", lambda r: None)
    assert gate_outcomes.record_gate_outcome("qa", SimpleNamespace(approved=True)) is False


def test_record_gate_outcome_emits_event_and_learning(monkeypatch) -> None:
    """A rejected gate emits a GATE_REJECTED event and ingests a learning from the first issue."""
    events: list = []
    learnings: list = []
    from software_engineering_team.shared import se_events

    monkeypatch.setattr(
        se_events, "record_event", lambda etype, **kw: events.append((etype, kw)) or True
    )
    monkeypatch.setattr(
        learnings_store, "upsert_learning", lambda **kw: learnings.append(kw) or True
    )
    result = SimpleNamespace(
        approved=False,
        summary="needs fixes",
        issues=[SimpleNamespace(description="missing null check", recommendation="add guard")],
    )
    ok = gate_outcomes.record_gate_outcome("code_review", result, job_id="j1", task_id="t1")
    assert ok is True
    assert events and events[0][0] == se_events.GATE_REJECTED
    assert learnings and learnings[0]["category"] == "code_review"
    assert learnings[0]["trigger"] == "missing null check"
    assert learnings[0]["counter_measure"] == "add guard"


def test_record_gate_outcome_prefers_failing_criterion(monkeypatch) -> None:
    """The learning trigger is taken from the failing acceptance criterion, not the summary."""
    learnings: list = []
    from software_engineering_team.shared import se_events

    monkeypatch.setattr(se_events, "record_event", lambda *a, **k: True)
    monkeypatch.setattr(
        learnings_store, "upsert_learning", lambda **kw: learnings.append(kw) or True
    )
    result = SimpleNamespace(
        all_satisfied=False,
        summary="2 of 3 met",
        per_criterion=[
            SimpleNamespace(criterion="A", satisfied=True),
            SimpleNamespace(criterion="B login works", satisfied=False),
        ],
    )
    gate_outcomes.record_gate_outcome("acceptance", result)
    assert learnings[0]["trigger"] == "B login works"


def test_record_gate_outcome_does_not_mislabel_passing_criterion(monkeypatch) -> None:
    """When all listed criteria passed, the trigger falls back to the summary, not a passing one."""
    # all_satisfied=False but every listed criterion passed → fall back to the
    # summary, never label a satisfied criterion as the failure.
    learnings: list = []
    from software_engineering_team.shared import se_events

    monkeypatch.setattr(se_events, "record_event", lambda *a, **k: True)
    monkeypatch.setattr(
        learnings_store, "upsert_learning", lambda **kw: learnings.append(kw) or True
    )
    result = SimpleNamespace(
        all_satisfied=False,
        summary="overall gate failed",
        per_criterion=[
            SimpleNamespace(criterion="A passes", satisfied=True),
            SimpleNamespace(criterion="B passes", satisfied=True),
        ],
    )
    gate_outcomes.record_gate_outcome("acceptance", result)
    assert learnings[0]["trigger"] == "overall gate failed"


def test_first_issue_returns_passing_only_for_plain_issue_lists() -> None:
    """_first_issue surfaces items[0] for plain issue lists but returns None when all criteria pass."""
    # Plain issue lists (no `satisfied` attr) still surface items[0].
    result = SimpleNamespace(
        issues=[SimpleNamespace(description="first"), SimpleNamespace(description="second")]
    )
    assert gate_outcomes._first_issue(result).description == "first"
    # All-satisfied per_criterion → None (no failing entry).
    crit = SimpleNamespace(per_criterion=[SimpleNamespace(criterion="ok", satisfied=True)])
    assert gate_outcomes._first_issue(crit) is None


# --- Tech Lead learnings block ---------------------------------------------


def test_learnings_top_n_parsing(monkeypatch) -> None:
    """_learnings_top_n defaults to 5, parses overrides, and clamps garbage and the ceiling."""
    monkeypatch.delenv("SE_LEARNINGS_TOPN", raising=False)
    assert _learnings_top_n() == 5
    monkeypatch.setenv("SE_LEARNINGS_TOPN", "3")
    assert _learnings_top_n() == 3
    monkeypatch.setenv("SE_LEARNINGS_TOPN", "garbage")
    assert _learnings_top_n() == 5
    monkeypatch.setenv("SE_LEARNINGS_TOPN", "999")
    assert _learnings_top_n() == 50


def _fake_input() -> SimpleNamespace:
    return SimpleNamespace(
        requirements=SimpleNamespace(title="Auth", description="Login feature"),
        architecture=SimpleNamespace(overview="FastAPI + Angular"),
        spec_content="users can log in",
    )


def test_relevant_learnings_block_empty(monkeypatch) -> None:
    """The Tech Lead learnings block is empty when no learnings are retrieved."""
    monkeypatch.setattr(learnings_store, "retrieve_learnings", lambda *a, **k: [])
    block = TechLeadAgent._relevant_learnings_block(_fake_input())
    assert block == []


def test_relevant_learnings_block_formats(monkeypatch) -> None:
    """The Tech Lead learnings block renders a header and each retrieved learning's text."""
    monkeypatch.setattr(
        learnings_store,
        "retrieve_learnings",
        lambda *a, **k: [
            Learning(
                pattern="security rejection",
                trigger="hardcoded secret",
                counter_measure="use env var",
                source="gate_rejection",
                category="security",
                occurrences=3,
            )
        ],
    )
    block = TechLeadAgent._relevant_learnings_block(_fake_input())
    assert any("RELEVANT LEARNINGS FROM PAST SPRINTS" in line for line in block)
    assert any("security rejection" in line and "use env var" in line for line in block)


def test_relevant_learnings_block_disabled(monkeypatch) -> None:
    """Setting SE_LEARNINGS_TOPN to 0 disables the Tech Lead learnings block."""
    monkeypatch.setenv("SE_LEARNINGS_TOPN", "0")
    block = TechLeadAgent._relevant_learnings_block(_fake_input())
    assert block == []
