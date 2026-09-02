"""Focused unit tests closing the shared-infra coverage gaps.

These pure-function branches (devops error parsers, model coercions, the
unbudgeted repo scanner, env/truncation helpers) were previously measured under
the SE team gate; after the extraction they are gated by the shared-infra CI
job, which these tests keep above the 90% floor.

``shared.hitl.temporal_signal`` (below) is a different flavor of gap: it was
extracted straight into ``shared/hitl/`` rather than migrated out of SE, and
its only consumer today is ``planning_team.temporal.workflows.PlanningWorkflow``
-- Planning has no dedicated CI test job of its own (its ``tests/`` directory
isn't in this workflow's per-team matrix), so nothing else in CI exercises it.
These tests keep it above the 90% floor until either Planning gains its own
CI job or ``CodingTeamWorkflow`` migrates onto this shared primitive (see
``shared/hitl/temporal_signal.py``'s module docstring).

More thorough, standalone-mixin behavioral coverage lives in
``shared/hitl/tests/test_temporal_signal.py`` and
``planning_team/tests/test_temporal_workflow_signal.py`` -- useful for local
development and as documentation, but neither runs in CI today for the same
reason (no CI job passes ``shared/hitl/tests/`` or ``planning_team/tests/``
to pytest). This file is therefore the only CI-enforced regression coverage
for this behavior, so it also carries a minimal check that the real
``PlanningWorkflow`` class (not just the standalone mixin) actually wires up
correctly -- the composition-level assertions those uncollected suites would
otherwise be the sole guard for.
"""

from __future__ import annotations

import logging
from pathlib import Path

import shared.hitl.temporal_signal as _temporal_signal_module
from shared.command_runner.error_parsing import (
    FailureClass,
    build_agent_feedback,
    get_failure_class_tag,
    log_failure,
    normalize_error_signature,
    parse_command_failure,
    parse_devops_failure,
)
from shared.dev_models import ToolRecommendation, model_to_dict
from shared.hitl.temporal_signal import MAX_BUFFERED_SIGNALS, HitlAnswerSignalMixin
from shared.repo_context.repo_utils import (
    int_env,
    read_repo_code,
    read_repo_files_as_dict,
    truncate_for_context,
)

# ---------------------------------------------------------------------------
# error_parsing
# ---------------------------------------------------------------------------


def test_normalize_error_signature_collapses_volatile_fragments() -> None:
    raw = (
        "error in /tmp/tmpab12cd34/main.py at 2026-07-03T12:00:01 took 1.23s "
        "object at 0x7f00deadbeef host localhost:54321   spaced"
    )
    sig = normalize_error_signature(raw)
    assert "0x7f00deadbeef" not in sig
    assert "54321" not in sig
    assert "  " not in sig  # whitespace collapsed
    assert normalize_error_signature(raw) == sig  # deterministic


def test_parse_devops_failure_yaml() -> None:
    failures = parse_devops_failure("yaml.parser.ParserError: while parsing a block mapping")
    assert failures and failures[0].failure_class == FailureClass.YAML_PARSE_ERROR


def test_parse_devops_failure_docker_copy() -> None:
    failures = parse_devops_failure('COPY failed: file not found in build context: "app/"')
    assert failures and failures[0].failure_class == FailureClass.DOCKER_BUILD_ERROR


def test_parse_devops_failure_failed_to_solve() -> None:
    failures = parse_devops_failure("failed to solve: base image pull denied")
    assert failures and failures[0].failure_class == FailureClass.DOCKER_BUILD_ERROR


def test_parse_devops_failure_run_command() -> None:
    failures = parse_devops_failure(
        "The command `/bin/sh -c pip install nope` returned a non-zero code: 1. package missing"
    )
    assert failures and failures[0].failure_class == FailureClass.DOCKER_BUILD_ERROR


def test_parse_devops_failure_generic_and_empty() -> None:
    generic = parse_devops_failure("docker build exploded for reasons")
    assert generic and generic[0].failure_class in (
        FailureClass.DOCKER_BUILD_ERROR,
        FailureClass.UNKNOWN,
    )
    assert parse_devops_failure("") == [] or parse_devops_failure("")[0] is not None


def test_parse_command_failure_dispatch() -> None:
    devops = parse_command_failure("devops", "", "failed to solve: nope")
    assert devops and devops[0].failure_class == FailureClass.DOCKER_BUILD_ERROR
    unknown = parse_command_failure("mystery-kind", "out", "err")
    assert unknown and unknown[0].failure_class == FailureClass.UNKNOWN


def test_build_agent_feedback_renders_sections() -> None:
    failures = parse_command_failure("devops", "", "failed to solve: nope")
    text = build_agent_feedback(failures)
    assert "docker" in text.lower() or "build" in text.lower()
    assert "Suggestion:" in text
    assert build_agent_feedback([]) == ""


def test_failure_class_tag_and_log(caplog) -> None:
    assert "yaml" in get_failure_class_tag(FailureClass.YAML_PARSE_ERROR).lower()
    with caplog.at_level(logging.INFO):
        log_failure(FailureClass.UNKNOWN, "something odd", task="t1")
    assert any("something odd" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# shared.dev_models
# ---------------------------------------------------------------------------


def _tool_kwargs(**overrides):
    base = dict(
        name="Postgres",
        category="database",
        description="db",
        rationale="solid",
        pricing_tier="free",
        pricing_details="free tier",
        license_type="mit",
        is_open_source=True,
    )
    base.update(overrides)
    return base


def test_tool_recommendation_cost_coercion() -> None:
    assert (
        ToolRecommendation(**_tool_kwargs(estimated_monthly_cost=0)).estimated_monthly_cost == "$0"
    )
    assert (
        ToolRecommendation(**_tool_kwargs(estimated_monthly_cost=12.5)).estimated_monthly_cost
        == "$12.50"
    )
    assert (
        ToolRecommendation(**_tool_kwargs(estimated_monthly_cost=7)).estimated_monthly_cost == "$7"
    )
    assert (
        ToolRecommendation(**_tool_kwargs(estimated_monthly_cost="$3-5")).estimated_monthly_cost
        == "$3-5"
    )
    assert ToolRecommendation(**_tool_kwargs()).estimated_monthly_cost is None


def test_model_to_dict_variants() -> None:
    rec = ToolRecommendation(**_tool_kwargs())
    assert model_to_dict(rec)["name"] == "Postgres"
    assert model_to_dict(None) == {}

    class _V1Like:
        def dict(self):
            return {"v": 1}

    assert model_to_dict(_V1Like()) == {"v": 1}

    class _Plain:
        pass

    plain = _Plain()
    plain.x = 5
    assert model_to_dict(plain) == {"x": 5}


# ---------------------------------------------------------------------------
# shared.repo_context
# ---------------------------------------------------------------------------


def test_read_repo_code_scans_and_excludes(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("A = 1")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.py").write_text("DEP = 1")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "hook.py").write_text("HOOK = 1")
    out = read_repo_code(tmp_path, [".py"])
    assert "app.py" in out and "A = 1" in out
    assert "dep.py" not in out  # default exclude set
    assert "hook.py" not in out  # .git always excluded
    assert read_repo_code(tmp_path, [".java"]) == "# No code files found"


def test_read_repo_files_as_dict_walks_and_skips_sensitive(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("M = 1")
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("x")
    sensitive: list[str] = []
    result = read_repo_files_as_dict(tmp_path, sensitive_skipped=sensitive)
    assert "main.py" in result
    assert all("node_modules" not in k for k in result)
    assert ".env" not in result and any(".env" in s for s in sensitive)


def test_truncate_for_context_paths() -> None:
    assert truncate_for_context("", 10) == ""
    assert truncate_for_context("short", 10) == "short"
    # No LLM available: over-budget text passes through untruncated by contract.
    assert truncate_for_context("x" * 20, 10) == "x" * 20

    calls = {}

    class _FakeCompactor:
        pass

    def _fake_compact(text, max_chars, llm, desc):
        calls["args"] = (len(text), max_chars, desc)
        return text[:max_chars]

    import llm_service

    original = llm_service.compact_text
    llm_service.compact_text = _fake_compact
    try:
        out = truncate_for_context("y" * 30, 10, llm=_FakeCompactor(), content_description="spec")
    finally:
        llm_service.compact_text = original
    assert out == "y" * 10
    assert calls["args"] == (30, 10, "spec")


def test_int_env_parses_and_clamps(monkeypatch) -> None:
    monkeypatch.delenv("SHARED_INFRA_TEST_INT", raising=False)
    assert int_env("SHARED_INFRA_TEST_INT", 7) == 7
    monkeypatch.setenv("SHARED_INFRA_TEST_INT", "42")
    assert int_env("SHARED_INFRA_TEST_INT", 7) == 42
    monkeypatch.setenv("SHARED_INFRA_TEST_INT", "0")
    assert int_env("SHARED_INFRA_TEST_INT", 7, min_val=1) == 1
    monkeypatch.setenv("SHARED_INFRA_TEST_INT", "garbage")
    assert int_env("SHARED_INFRA_TEST_INT", 7) == 7


# ---------------------------------------------------------------------------
# shared.hitl.temporal_signal
# ---------------------------------------------------------------------------


class _Workflow(HitlAnswerSignalMixin):
    """Minimal stand-in for a real ``@workflow.defn`` class mixing this in."""


def _answer(question_id: str = "q1") -> dict:
    return {"question_id": question_id, "selected_option_id": None, "other_text": None}


def test_hitl_signal_mixin_init_state() -> None:
    wf = _Workflow()
    assert wf._active_resume_token is None
    assert wf._submitted_answers is None
    assert wf._buffered_signals == {}


def test_hitl_signal_mixin_rejects_malformed_answer_batch() -> None:
    """One malformed answer entry rejects the whole batch (fails closed) rather
    than resuming with partial content."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1", "answers": ["not-a-dict"]})
    assert wf._submitted_answers is None

    wf.submit_answers(
        {
            "resume_token": "tok-1",
            "answers": [_answer("q1"), {"selected_option_id": "no-question-id"}],
        }
    )
    assert wf._submitted_answers is None

    wf.submit_answers({"resume_token": "tok-1", "answers": []})
    assert wf._submitted_answers is None

    wf.submit_answers({"resume_token": "tok-1", "answers": [{1: "x", "question_id": "q1"}]})
    assert wf._submitted_answers is None


def test_hitl_signal_mixin_accepts_matching_signal() -> None:
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1", "answers": [_answer("q1")]})

    assert wf._submitted_answers == [_answer("q1")]


def test_hitl_signal_mixin_ignores_non_dict_payload() -> None:
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers("not-a-dict")

    assert wf._submitted_answers is None


def test_hitl_signal_mixin_tolerates_zero_argument_delivery() -> None:
    """A zero-arg signal delivery (handler.fn(*decoded_args) with no args) must
    bind the payload: Any = None default and fall through to the non-dict
    rejection rather than raising TypeError for a missing required argument,
    which would permanently strand the workflow on replay."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers()

    assert wf._submitted_answers is None
    assert wf._buffered_signals == {}


def test_hitl_signal_mixin_rejects_answer_entry_with_unrecognized_key() -> None:
    """An unrecognized key (e.g. a misspelled field name) must reject the whole
    batch -- pydantic's default model_dump() would otherwise silently drop it,
    letting a typo'd submission "succeed" with its actual content stripped."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers(
        {"resume_token": "tok-1", "answers": [{"question_id": "q1", "other_txt": "typo"}]}
    )

    assert wf._submitted_answers is None


def test_hitl_signal_mixin_logs_diagnostic_inside_a_workflow(monkeypatch) -> None:
    """_log_signal_diagnostic's in-workflow branch (guarded by
    workflow.in_workflow()) is only reachable inside a real Temporal workflow
    sandbox -- monkeypatch it here so the operator diagnostic trail this
    module's postconditions promise is actually exercised by CI."""

    class _FakeLogger:
        def __init__(self) -> None:
            self.warnings: list[tuple] = []

        def warning(self, msg, *args) -> None:
            self.warnings.append((msg, args))

    fake_logger = _FakeLogger()
    monkeypatch.setattr(_temporal_signal_module.workflow, "in_workflow", lambda: True)
    monkeypatch.setattr(_temporal_signal_module.workflow, "logger", fake_logger)

    _temporal_signal_module._log_signal_diagnostic("submit_answers rejected: %r", "reason")

    assert fake_logger.warnings == [("submit_answers rejected: %r", ("reason",))]


def test_hitl_signal_mixin_ignores_second_submission_for_same_token() -> None:
    wf = _Workflow()
    wf._active_resume_token = "tok-1"
    first = [_answer("q1")]
    wf.submit_answers({"resume_token": "tok-1", "answers": first})

    wf.submit_answers({"resume_token": "tok-1", "answers": [_answer("q2")]})

    assert wf._submitted_answers == first


def test_hitl_signal_mixin_rejects_out_of_order_signal() -> None:
    wf = _Workflow()
    wf._active_resume_token = "current-token"

    wf.submit_answers({"resume_token": "stale-token", "answers": [_answer("q1")]})

    assert wf._submitted_answers is None


def test_hitl_signal_mixin_buffers_and_evicts_past_cap() -> None:
    wf = _Workflow()
    for i in range(MAX_BUFFERED_SIGNALS + 1):
        wf.submit_answers({"resume_token": f"tok-{i}", "answers": [_answer("q1")]})

    assert len(wf._buffered_signals) == MAX_BUFFERED_SIGNALS
    assert "tok-0" not in wf._buffered_signals


def test_hitl_signal_mixin_drops_early_signal_with_no_usable_resume_token() -> None:
    wf = _Workflow()

    wf.submit_answers({"resume_token": "", "answers": [_answer("q1")]})
    wf.submit_answers({"answers": [_answer("q1")]})

    assert wf._buffered_signals == {}


# ---------------------------------------------------------------------------
# planning_team.temporal.workflows.PlanningWorkflow composition
# ---------------------------------------------------------------------------
# CI-enforced (unlike planning_team/tests/test_temporal_workflow_signal.py,
# see module docstring): proves the real PlanningWorkflow class -- not just
# the standalone mixin above -- actually composes HitlAnswerSignalMixin.


def test_planning_workflow_registers_submit_answers_signal() -> None:
    from temporalio import workflow as _workflow

    from planning_team.temporal.workflows import PlanningWorkflow
    from shared.hitl.temporal_signal import SUBMIT_ANSWERS_SIGNAL

    # temporalio private API (_Definition); re-verify on temporalio upgrades.
    defn = _workflow._Definition.from_class(PlanningWorkflow)
    assert defn is not None, "PlanningWorkflow is missing the @workflow.defn decorator"
    # Pin the literal, not just the constant: a changed SUBMIT_ANSWERS_SIGNAL value
    # would otherwise pass CI while breaking the #7451-specified wire contract.
    assert SUBMIT_ANSWERS_SIGNAL == "submit_answers"
    assert SUBMIT_ANSWERS_SIGNAL in defn.signals


def test_planning_workflow_submit_answers_accepts_and_rejects() -> None:
    from planning_team.temporal.workflows import PlanningWorkflow

    wf = PlanningWorkflow()
    assert wf._active_resume_token is None

    wf._active_resume_token = "tok-1"
    wf.submit_answers({"resume_token": "stale-token", "answers": [_answer("q1")]})
    assert wf._submitted_answers is None
    assert wf._buffered_signals == {}

    wf.submit_answers({"resume_token": "tok-1", "answers": [_answer("q1")]})
    assert wf._submitted_answers == [_answer("q1")]
