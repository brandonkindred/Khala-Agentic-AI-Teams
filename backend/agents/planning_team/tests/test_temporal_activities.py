"""Unit tests for the Planning per-phase Temporal activities.

The workflow body + worker bootstrap only run against a live Temporal cluster
(and are omitted from coverage), but the activity wrappers in
``planning_team.temporal.activities`` are pure Python: each drives one phase
function, normalizes the JSON boundary, and writes job-store progress. These
tests drive them directly (no worker), assert their per-phase behavior, and — the
key guard — run the whole activity sequence in-process and prove it produces the
same handoff as the in-process orchestrator (``run_workflow``), so the two
dispatch paths cannot silently diverge.
"""

import sys
from pathlib import Path

import pytest

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from planning_team.temporal import activities as A  # noqa: E402

from .conftest import make_llm  # noqa: E402

# A single LLM response that satisfies BOTH the discovery map (problem/opportunity/
# users/criteria) and the requirements map (questions); each phase reads only the
# keys it needs, so one return_value keeps both phases deterministic.
_LLM_JSON = (
    '{"problem_summary": "Need X", "opportunity_statement": "Y", '
    '"target_users": ["u1"], "success_criteria": ["c1"], "assumptions": [], '
    '"questions": [{"id": "q1", "question_text": "Scope?", '
    '"options": [{"id": "o1", "label": "A", "is_default": true}]}]}'
)


@pytest.fixture
def job_store(monkeypatch):
    """Stateful in-memory fake of the durable job store.

    Records every write (``update``/``completed``/``failed`` call lists) and keeps
    the merged job state in ``jobs`` so ``get_job`` round-trips what was persisted
    (document production writes the handoff; sub-agent provisioning reads it back).
    """
    from planning_team.shared import job_store as js

    state: dict = {"jobs": {}, "update": [], "completed": [], "failed": []}

    def _update(job_id, **fields):
        state["update"].append((job_id, fields))
        state["jobs"].setdefault(job_id, {}).update(fields)

    def _get(job_id):
        job = state["jobs"].get(job_id)
        return dict(job) if job is not None else None

    def _completed(job_id, **fields):
        state["completed"].append((job_id, fields))
        state["jobs"].setdefault(job_id, {}).update(fields, status="completed")

    def _failed(job_id, error):
        state["failed"].append((job_id, error))
        state["jobs"].setdefault(job_id, {}).update(error=error, status="failed")

    monkeypatch.setattr(js, "update_job", _update)
    monkeypatch.setattr(js, "get_job", _get)
    monkeypatch.setattr(js, "mark_job_completed", _completed)
    monkeypatch.setattr(js, "mark_job_failed", _failed)
    return state


@pytest.fixture
def dummy_llm(monkeypatch):
    """Route ``get_client('planning')`` to a deterministic fake LLM client."""
    llm = make_llm(_LLM_JSON)
    monkeypatch.setattr("llm_service.get_client", lambda agent_key=None: llm)
    return llm


# --------------------------------------------------------------------------- #
# Per-phase activities
# --------------------------------------------------------------------------- #


def test_intake_activity_flips_running_and_seeds_context(tmp_path, job_store):
    ctx = A.intake_activity("job-1", str(tmp_path), "Acme", "a brief", None)

    assert ctx["repo_path"] == str(tmp_path)
    assert ctx["initial_brief"] == "a brief"
    # client_context crosses the boundary as a JSON dict, not a pydantic object.
    assert isinstance(ctx["client_context"], dict)
    assert ctx["client_context"]["client_name"] == "Acme"
    # First activity flips the job to RUNNING at 5%.
    job_id, fields = job_store["update"][0]
    assert job_id == "job-1"
    assert fields["status"] == "running"
    assert fields["progress"] == 5
    assert fields["current_phase"] == "intake"


def test_discovery_activity_refines_client_context(tmp_path, job_store, dummy_llm):
    ctx = A.intake_activity("job-1", str(tmp_path), "Acme", "brief", None)
    ctx = A.discovery_activity("job-1", ctx)

    assert dummy_llm.complete_text.called
    assert ctx["client_context"]["problem_summary"] == "Need X"
    # The discovery progress write reports its phase/progress; non-intake phases
    # deliberately leave `status` untouched (so a concurrent cancel isn't clobbered).
    discovery_updates = [
        f for jid, f in job_store["update"] if f.get("current_phase") == "discovery"
    ]
    assert discovery_updates
    assert discovery_updates[0]["progress"] == 15
    assert discovery_updates[0]["status_text"] == "Discovery"
    assert "status" not in discovery_updates[0]


def test_requirements_activity_adds_open_questions(tmp_path, job_store, dummy_llm):
    ctx = A.intake_activity("job-1", str(tmp_path), "Acme", "brief", None)
    ctx = A.discovery_activity("job-1", ctx)
    ctx = A.requirements_activity("job-1", ctx)

    assert isinstance(ctx["open_questions"], list) and ctx["open_questions"]
    # Questions cross the boundary as JSON dicts, not OpenQuestion objects.
    assert all(isinstance(q, dict) for q in ctx["open_questions"])


def test_market_research_activity_returns_evidence(monkeypatch, job_store):
    monkeypatch.setattr(
        "planning_team.adapters.request_market_research",
        lambda **kw: {"raw": "data"},
    )
    monkeypatch.setattr(
        "planning_team.adapters.market_research_to_evidence",
        lambda data: {"summary": "S", "insights": ["i1"]},
    )
    ctx = {"client_context": {"problem_summary": "Need X", "target_users": ["u1"]}}

    evidence = A.market_research_activity("job-1", ctx)

    assert evidence == {"summary": "S", "insights": ["i1"]}


def test_market_research_activity_none_when_nothing_to_research(job_store):
    # No problem and no users → nothing to research; returns None (no adapter call).
    assert A.market_research_activity("job-1", {"client_context": {}}) is None


def test_market_research_activity_none_when_team_returns_nothing(monkeypatch, job_store):
    monkeypatch.setattr("planning_team.adapters.request_market_research", lambda **kw: None)
    ctx = {"client_context": {"problem_summary": "Need X"}}
    assert A.market_research_activity("job-1", ctx) is None


def test_synthesis_activity_folds_in_evidence(tmp_path, job_store):
    ctx = A.intake_activity("job-1", str(tmp_path), "Acme", "brief", None)
    evidence = {"summary": "MR summary", "insights": ["insight"]}

    ctx = A.synthesis_activity("job-1", ctx, evidence)

    assert ctx["market_research_evidence"] == evidence
    cc = ctx["client_context"]
    assert cc["constraints"]["market_research_summary"] == "MR summary"


def test_synthesis_activity_noop_without_evidence(tmp_path, job_store):
    ctx = A.intake_activity("job-1", str(tmp_path), "Acme", "brief", None)
    out = A.synthesis_activity("job-1", ctx, None)
    assert "market_research_evidence" not in out


def test_document_production_activity_no_pra(tmp_path, job_store):
    ctx = A.intake_activity("job-1", str(tmp_path), "Acme", "brief", None)
    ctx = A.document_production_activity("job-1", ctx, False)

    # The activity result is slim; the handoff is persisted to the job store.
    assert ctx == {"repo_path": str(tmp_path)}
    handoff = job_store["jobs"]["job-1"]["handoff_package"]
    assert isinstance(handoff, dict)
    # No PRA → the initial spec is the validated spec.
    assert handoff["validated_spec_path"].endswith("initial_spec.md")
    assert handoff["client_context"]["client_name"] == "Acme"


def test_document_production_activity_with_pra(tmp_path, monkeypatch, job_store):
    monkeypatch.setattr("planning_team.adapters.run_product_analysis", lambda **kw: "pra-job")

    answered = {}

    def _fake_wait(*, job_id, answer_callback):
        # Exercise the auto-answer path: PRA surfaces a clarification question and
        # the activity's callback resolves it with the default option (no user).
        answered["result"] = answer_callback(
            [{"id": "q1", "options": [{"id": "o1", "is_default": True}]}]
        )
        return {"status": "completed"}

    monkeypatch.setattr("planning_team.adapters.wait_for_product_analysis_completion", _fake_wait)
    ctx = A.intake_activity("job-1", str(tmp_path), "Acme", "brief", None)
    ctx = A.document_production_activity("job-1", ctx, True)

    handoff = job_store["jobs"]["job-1"]["handoff_package"]
    assert handoff["validated_spec_path"].endswith("validated_spec.md")
    assert handoff["prd_path"].endswith("product_requirements_document.md")
    # The auto-answer callback picked the default option (parity with the HTTP path).
    assert answered["result"] == [{"question_id": "q1", "selected_option_id": "o1"}]


def test_sub_agent_provisioning_activity_noop_without_gap(tmp_path, job_store):
    ctx = A.intake_activity("job-1", str(tmp_path), "Acme", "brief", None)
    ctx = A.document_production_activity("job-1", ctx, False)
    before = dict(job_store["jobs"]["job-1"]["handoff_package"])

    ctx = A.sub_agent_provisioning_activity("job-1", ctx, None)

    # No capability gap → no blueprint attached; the persisted handoff is unchanged
    # (its sub_agent_blueprint field stays at its None default).
    assert "sub_agent_blueprint" not in ctx
    handoff = job_store["jobs"]["job-1"]["handoff_package"]
    assert handoff["sub_agent_blueprint"] is None
    assert handoff == before


def test_sub_agent_provisioning_activity_attaches_blueprint(tmp_path, monkeypatch, job_store):
    monkeypatch.setattr("planning_team.adapters.start_ai_systems_build", lambda **kw: "ai-job")
    monkeypatch.setattr(
        "planning_team.adapters.wait_for_ai_systems_build_completion",
        lambda **kw: {"status": "completed", "blueprint": {"name": "agent-x"}},
    )
    ctx = A.intake_activity("job-1", str(tmp_path), "Acme", "brief", None)
    ctx = A.document_production_activity("job-1", ctx, False)

    ctx = A.sub_agent_provisioning_activity("job-1", ctx, "needs a scraper")

    assert ctx["sub_agent_blueprint"] == {"name": "agent-x"}
    # The blueprint is attached to the persisted handoff, not the activity result.
    assert job_store["jobs"]["job-1"]["handoff_package"]["sub_agent_blueprint"] == {
        "name": "agent-x"
    }


def test_finalize_activity_marks_completed(job_store):
    # Handoff was already persisted by document production; finalize must not
    # re-pass (and thus clobber) it — it only flips status/summary.
    job_store["jobs"]["job-1"] = {"handoff_package": {"summary": "hp"}}

    result = A.finalize_planning_activity("job-1", {"repo_path": "/x"})

    assert result == {"success": True, "summary": "Planning completed; handoff package ready."}
    job_id, fields = job_store["completed"][0]
    assert job_id == "job-1"
    assert "handoff_package" not in fields
    assert fields["summary"] == "Planning completed; handoff package ready."
    # The previously-persisted handoff survives the completion write.
    assert job_store["jobs"]["job-1"]["handoff_package"] == {"summary": "hp"}
    assert job_store["jobs"]["job-1"]["status"] == "completed"


def test_finalize_activity_records_planning_run(monkeypatch, job_store) -> None:
    """finalize re-reads the persisted handoff and calls record_planning_run
    with the audit columns derived from it."""
    job_store["jobs"]["job-1"] = {
        "handoff_package": {
            "client_context": {"client_name": "Acme"},
            "summary": "Handoff package produced by Planning.",
            "open_questions": [{"id": "q1"}],
            "resolved_questions": [{"question_id": "q1"}],
        }
    }
    captured = {}
    monkeypatch.setattr(
        "planning_team.postgres.writer.record_planning_run",
        lambda job_id, **kwargs: captured.setdefault("call", (job_id, kwargs)) or True,
    )

    result = A.finalize_planning_activity("job-1", {"repo_path": "/x"})

    assert result == {"success": True, "summary": "Planning completed; handoff package ready."}
    job_id, kwargs = captured["call"]
    assert job_id == "job-1"
    assert kwargs["client_name"] == "Acme"
    assert kwargs["summary"] == "Planning completed; handoff package ready."
    assert kwargs["handoff_summary"] == "Handoff package produced by Planning."
    assert kwargs["open_questions"] == [{"id": "q1"}]
    assert kwargs["resolved_questions"] == [{"question_id": "q1"}]


def test_finalize_activity_audit_read_failure_does_not_fail_finalize(
    monkeypatch, job_store
) -> None:
    """A live get_job failure during the audit re-read (unlike record_planning_run,
    which never raises) must not escape _work, retry via _guarded, or turn the
    already-completed job into a failure."""
    job_store["jobs"]["job-1"] = {"handoff_package": {"summary": "hp"}}

    def _raise_get_job(job_id):
        raise RuntimeError("job service unreachable")

    monkeypatch.setattr("planning_team.shared.job_store.get_job", _raise_get_job)

    result = A.finalize_planning_activity("job-1", {"repo_path": "/x"})

    assert result == {"success": True, "summary": "Planning completed; handoff package ready."}
    assert job_store["completed"][0][0] == "job-1"
    assert job_store["failed"] == []


def test_legacy_run_planning_activity_delegates(monkeypatch):
    """The legacy single-activity path (kept for rollout replay) delegates to the
    same run_workflow_background the thread-mode /run endpoint uses."""
    import planning_team.api.main as api_main

    captured = {}
    monkeypatch.setattr(
        api_main, "run_workflow_background", lambda *args: captured.setdefault("args", args)
    )

    A.run_planning_activity("job-1", "/tmp/ws", "Acme", "brief", None, True, False)

    assert captured["args"] == ("job-1", "/tmp/ws", "Acme", "brief", None, True, False)


def test_legacy_run_planning_activity_reraises(monkeypatch):
    """A legacy-path failure re-raises so the legacy RetryPolicy governs retries."""
    import planning_team.api.main as api_main

    def _boom(*args):
        raise RuntimeError("legacy boom")

    monkeypatch.setattr(api_main, "run_workflow_background", _boom)

    with pytest.raises(RuntimeError, match="legacy boom"):
        A.run_planning_activity("job-1", "/tmp/ws", None, "brief", None, True, False)


def test_activity_marks_job_failed_and_reraises(tmp_path, monkeypatch, job_store):
    """A phase error marks the job FAILED (so it can't look 'completed') and
    re-raises so Temporal sees a failed activity."""

    def _boom(agent_key=None):
        raise RuntimeError("no LLM configured")

    monkeypatch.setattr("llm_service.get_client", _boom)
    ctx = A.intake_activity("job-1", str(tmp_path), "Acme", "brief", None)

    with pytest.raises(RuntimeError, match="no LLM configured"):
        A.discovery_activity("job-1", ctx)

    assert job_store["failed"] == [("job-1", "no LLM configured")]


def test_progress_write_failure_marks_job_failed(monkeypatch, job_store):
    """The progress write is INSIDE the guard, so a failing update_job (e.g. a
    job-store blip) still marks the job FAILED instead of leaving it stuck."""
    from planning_team.shared import job_store as js

    def _boom_update(job_id, **fields):
        raise RuntimeError("job store down")

    monkeypatch.setattr(js, "update_job", _boom_update)

    with pytest.raises(RuntimeError, match="job store down"):
        A.discovery_activity("job-1", {"client_context": {}})

    assert job_store["failed"] == [("job-1", "job store down")]


def test_non_final_attempt_does_not_mark_failed(monkeypatch, job_store):
    """On a non-final Temporal attempt, a retryable phase's failure does NOT mark
    the job FAILED (Temporal will retry) — only the final attempt marks it, so a
    retry that later succeeds never leaves a transient FAILED / stale error."""

    class _Info:
        attempt = 1  # discovery is SAFE_RETRY (max 3) → attempt 1 is not final

    monkeypatch.setattr(A.activity, "in_activity", lambda: True)
    monkeypatch.setattr(A.activity, "info", lambda: _Info())

    def _boom(agent_key=None):
        raise RuntimeError("transient")

    monkeypatch.setattr("llm_service.get_client", _boom)

    with pytest.raises(RuntimeError, match="transient"):
        A.discovery_activity("job-1", {"client_context": {}})

    assert job_store["failed"] == []


# --------------------------------------------------------------------------- #
# Parity: the activity sequence == the in-process orchestrator
# --------------------------------------------------------------------------- #


def _run_activity_sequence(job_id, repo_path):
    ctx = A.intake_activity(job_id, repo_path, "Acme", "a brief", None)
    ctx = A.discovery_activity(job_id, ctx)
    ctx = A.requirements_activity(job_id, ctx)
    ctx = A.synthesis_activity(job_id, ctx, None)
    ctx = A.document_production_activity(job_id, ctx, False)
    ctx = A.sub_agent_provisioning_activity(job_id, ctx, None)
    A.finalize_planning_activity(job_id, ctx)
    return ctx


def test_activity_sequence_matches_orchestrator_handoff(tmp_path, monkeypatch, job_store):
    """The per-phase activities and ``run_workflow`` share the phase functions, so
    with the same inputs the resulting handoff's client context must be identical —
    the guard that keeps thread mode and Temporal mode from drifting."""
    from planning_team.orchestrator import run_workflow

    # In-process orchestrator (thread-mode path).
    orch_result = run_workflow(
        repo_path=str(tmp_path / "orch"),
        client_name="Acme",
        initial_brief="a brief",
        use_product_analysis=False,
        use_market_research=False,
        llm=make_llm(_LLM_JSON),
        job_updater=None,
    )

    # Activity sequence (Temporal-mode path) with the same fake LLM.
    monkeypatch.setattr("llm_service.get_client", lambda agent_key=None: make_llm(_LLM_JSON))
    _run_activity_sequence("job-1", str(tmp_path / "act"))
    # The Temporal path persists the handoff to the job store (not the context).
    act_handoff = job_store["jobs"]["job-1"]["handoff_package"]

    assert orch_result["success"] is True
    # client_context is path-independent, so it must match exactly across paths.
    assert act_handoff["client_context"] == orch_result["handoff_package"]["client_context"]
    assert act_handoff["client_context"]["problem_summary"] == "Need X"
    # The handoff's open/resolved questions must stay identical across the thread
    # and Temporal paths (both empty today — the SE gate pauses on non-empty
    # open_questions, so this empty handoff is deliberately preserved).
    assert act_handoff["open_questions"] == orch_result["handoff_package"]["open_questions"]
    assert act_handoff["resolved_questions"] == orch_result["handoff_package"]["resolved_questions"]


def test_activity_sequence_matches_orchestrator_handoff_with_pra_and_mr(
    tmp_path, monkeypatch, job_store
):
    """Second parity guard exercising the PRA + market-research branches through the
    FULL sequence (the other parity test covers the no-PRA/no-MR path), so a drift in
    how the orchestrator vs the activities compose those branches can't slip through.
    Both paths use the same mocked cross-team adapters."""
    from planning_team.orchestrator import run_workflow

    monkeypatch.setattr("planning_team.adapters.request_market_research", lambda **kw: {"raw": "x"})
    monkeypatch.setattr(
        "planning_team.adapters.market_research_to_evidence",
        lambda data: {"summary": "MR", "insights": ["i1"]},
    )
    monkeypatch.setattr("planning_team.adapters.run_product_analysis", lambda **kw: "pra-job")
    monkeypatch.setattr(
        "planning_team.adapters.wait_for_product_analysis_completion",
        lambda **kw: {"status": "completed"},
    )

    # Thread-mode orchestrator with both branches on.
    orch_result = run_workflow(
        repo_path=str(tmp_path / "orch"),
        client_name="Acme",
        initial_brief="a brief",
        use_product_analysis=True,
        use_market_research=True,
        llm=make_llm(_LLM_JSON),
        job_updater=None,
    )

    # Temporal-mode: drive the full activity sequence WITH the market-research and
    # PRA branches (market_research_activity → synthesis; document_production True).
    monkeypatch.setattr("llm_service.get_client", lambda agent_key=None: make_llm(_LLM_JSON))
    ctx = A.intake_activity("job-2", str(tmp_path / "act"), "Acme", "a brief", None)
    ctx = A.discovery_activity("job-2", ctx)
    ctx = A.requirements_activity("job-2", ctx)
    evidence = A.market_research_activity("job-2", ctx)
    ctx = A.synthesis_activity("job-2", ctx, evidence)
    ctx = A.document_production_activity("job-2", ctx, True)
    ctx = A.sub_agent_provisioning_activity("job-2", ctx, None)
    A.finalize_planning_activity("job-2", ctx)
    act_handoff = job_store["jobs"]["job-2"]["handoff_package"]

    assert orch_result["success"] is True
    # client_context (path-independent) must match, including the market-research
    # evidence synthesis folds into its constraints on BOTH paths.
    assert act_handoff["client_context"] == orch_result["handoff_package"]["client_context"]
    assert act_handoff["client_context"]["constraints"]["market_research_summary"] == "MR"
    # PRA ran on both paths → the validated spec resolves under product_analysis/.
    assert act_handoff["validated_spec_path"].endswith("validated_spec.md")
    assert orch_result["handoff_package"]["validated_spec_path"].endswith("validated_spec.md")
