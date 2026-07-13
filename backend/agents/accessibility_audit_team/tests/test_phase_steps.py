"""Tests for the shared per-phase step helpers in ``audit_execution``.

These are the single source of truth for one phase's work + partial-state
persistence, shared by thread mode (the orchestrator) and the Temporal per-phase
activities. They are exercised here directly (no Temporal runtime) with the phase
functions, ``_build_llm_client``, and ``persist``/``load`` stubbed, so the funnel
logic — keeping the API-supplied ``audit_id`` as the store key, recording each
phase, and raising when prior state is missing — is covered without a live cluster.
"""

from __future__ import annotations

import asyncio
import unittest.mock as mock

import pytest

from accessibility_audit_team import audit_execution as ax
from accessibility_audit_team.models import (
    AccessibilityAuditResult,
    DiscoveryResult,
    IntakeResult,
    Phase,
    ReportPackagingResult,
    VerificationResult,
)


@pytest.fixture(autouse=True)
def _isolate_steps(monkeypatch, tmp_path):
    """Stub the LLM client (no strands dependency) and redirect the artifact store to
    a per-test tmp dir, so the steps run the *real* ``persist_audit_state`` without a
    repo-dir side effect. ``load_audit_state`` is stubbed per-test via ``_seed``.

    ``get_job_manager`` is stubbed with a Mock whose ``get_job`` returns ``None`` by
    default (no real job-service network call, and "job not found" reads as
    "not terminal" to ``_persist_unless_job_terminal`` — the same as today's
    behavior of always persisting). Tests exercising the terminal-skip guard itself
    override ``jm.get_job.return_value`` locally.
    """
    import accessibility_audit_team.artifact_store as store_mod

    monkeypatch.setattr(ax, "_build_llm_client", lambda: object())
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    monkeypatch.setattr(store_mod, "_artifact_store", None)  # rebuild against tmp path
    jm = mock.Mock()
    jm.get_job.return_value = None
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)


# ---------------------------------------------------------------------------
# finalize_audit_result — severity counting + summary (real models)
# ---------------------------------------------------------------------------


def test_finalize_audit_result_counts_and_summary(sample_findings, sample_patterns):
    result = AccessibilityAuditResult(audit_id="a1")
    report = ReportPackagingResult(
        success=True, final_backlog=sample_findings, patterns=sample_patterns
    )

    out = ax.finalize_audit_result(result, report)
    assert out is result
    assert result.success is True
    assert result.total_findings == 3
    assert (result.critical_count, result.high_count, result.medium_count, result.low_count) == (
        1,
        1,
        1,
        0,
    )
    assert "3 findings" in result.summary
    assert result.completed_phases.count(Phase.REPORT_PACKAGING) == 1


def test_finalize_audit_result_is_idempotent_on_phase(sample_findings):
    result = AccessibilityAuditResult(audit_id="a1")
    report = ReportPackagingResult(success=True, final_backlog=sample_findings)
    ax.finalize_audit_result(result, report)
    ax.finalize_audit_result(result, report)
    assert result.completed_phases.count(Phase.REPORT_PACKAGING) == 1


# ---------------------------------------------------------------------------
# run_intake_step
# ---------------------------------------------------------------------------


def test_run_intake_step_keeps_api_audit_id(monkeypatch, sample_audit_plan):
    """The persisted state is keyed by the API-supplied audit_id even though the
    plan carries a different id — so the workflow's threaded key can reload it."""
    assert sample_audit_plan.audit_id == "audit_test01"  # differs from the API id below
    monkeypatch.setattr(
        ax,
        "run_intake_phase",
        mock.AsyncMock(return_value=IntakeResult(success=True, audit_plan=sample_audit_plan)),
    )

    result = asyncio.run(ax.run_intake_step("j1", "a1", ax.CreateAuditRequest(web_urls=[])))
    assert result.audit_id == "a1"
    assert result.intake_result is not None
    assert Phase.INTAKE in result.completed_phases
    # The real persist wrote under the API id, so it reloads by that key.
    assert asyncio.run(ax.load_audit_state("a1")).audit_id == "a1"


def test_run_intake_step_failure_sets_failure_reason(monkeypatch):
    monkeypatch.setattr(
        ax,
        "run_intake_phase",
        mock.AsyncMock(return_value=IntakeResult(success=False, error="APL failed")),
    )
    result = asyncio.run(ax.run_intake_step("j1", "a1", ax.CreateAuditRequest()))
    assert result.failure_reason == "APL failed"
    assert result.success is False
    assert Phase.INTAKE not in result.completed_phases


def test_run_intake_step_raises_when_persist_fails_on_logical_failure(monkeypatch):
    monkeypatch.setattr(
        ax,
        "run_intake_phase",
        mock.AsyncMock(return_value=IntakeResult(success=False, error="APL failed")),
    )
    monkeypatch.setattr(ax, "persist_audit_state", mock.AsyncMock(return_value=False))
    with pytest.raises(RuntimeError, match="failed to persist audit state"):
        asyncio.run(ax.run_intake_step("j1", "a1", ax.CreateAuditRequest()))


def test_run_intake_step_skips_rerun_when_already_complete(monkeypatch, sample_audit_plan):
    """An at-least-once Temporal retry after intake already succeeded and
    persisted (the completion ack to the server was lost) must not re-run the
    nondeterministic intake LLM call and overwrite the originally persisted
    audit plan/coverage matrix with a second, possibly-different one."""
    seeded = AccessibilityAuditResult(
        audit_id="a1",
        intake_result=IntakeResult(success=True, audit_plan=sample_audit_plan),
        completed_phases=[Phase.INTAKE],
    )
    asyncio.run(ax.persist_audit_state(seeded))
    phase_fn = mock.AsyncMock()
    monkeypatch.setattr(ax, "run_intake_phase", phase_fn)

    result = asyncio.run(ax.run_intake_step("j1", "a1", ax.CreateAuditRequest(web_urls=[])))

    phase_fn.assert_not_called()
    assert result.audit_id == "a1"
    assert result.completed_phases == [Phase.INTAKE]
    assert result.intake_result.audit_plan.audit_id == sample_audit_plan.audit_id


# ---------------------------------------------------------------------------
# run_discovery_step / run_verification_step / run_report_packaging_step
# ---------------------------------------------------------------------------


def _seed(monkeypatch, result: AccessibilityAuditResult):
    monkeypatch.setattr(ax, "_load_audit_state_strict", mock.AsyncMock(return_value=result))


def test_run_discovery_step_records_phase(monkeypatch, sample_audit_plan, sample_findings):
    _seed(
        monkeypatch,
        AccessibilityAuditResult(
            audit_id="a1", intake_result=IntakeResult(success=True, audit_plan=sample_audit_plan)
        ),
    )
    monkeypatch.setattr(
        ax,
        "run_discovery_phase",
        mock.AsyncMock(return_value=DiscoveryResult(success=True, draft_findings=sample_findings)),
    )
    result = asyncio.run(ax.run_discovery_step("j1", "a1"))
    assert result.discovery_result is not None
    assert Phase.DISCOVERY in result.completed_phases


def test_run_discovery_step_raises_when_state_missing(monkeypatch):
    monkeypatch.setattr(ax, "_load_audit_state_strict", mock.AsyncMock(return_value=None))
    with pytest.raises(RuntimeError, match="intake state"):
        asyncio.run(ax.run_discovery_step("j1", "a1"))


def test_run_verification_step_threads_tech_stack(monkeypatch, sample_findings):
    _seed(
        monkeypatch,
        AccessibilityAuditResult(
            audit_id="a1",
            discovery_result=DiscoveryResult(success=True, draft_findings=sample_findings),
        ),
    )
    captured: dict = {}

    async def _fake_verification(**kwargs):
        captured["stack"] = kwargs["stack"]
        return VerificationResult(success=True, verified_findings=sample_findings)

    monkeypatch.setattr(ax, "run_verification_phase", _fake_verification)
    result = asyncio.run(ax.run_verification_step("j1", "a1", {"web": "angular"}))
    assert captured["stack"] == {"web": "angular"}
    assert Phase.VERIFICATION in result.completed_phases


def test_run_verification_step_defaults_tech_stack(monkeypatch, sample_findings):
    _seed(
        monkeypatch,
        AccessibilityAuditResult(
            audit_id="a1",
            discovery_result=DiscoveryResult(success=True, draft_findings=sample_findings),
        ),
    )
    captured: dict = {}

    async def _fake_verification(**kwargs):
        captured["stack"] = kwargs["stack"]
        return VerificationResult(success=True)

    monkeypatch.setattr(ax, "run_verification_phase", _fake_verification)
    asyncio.run(ax.run_verification_step("j1", "a1", None))
    assert captured["stack"] == {"web": "other", "mobile": "other"}


def test_run_report_packaging_step_defers_finalize(monkeypatch, sample_findings):
    _seed(
        monkeypatch,
        AccessibilityAuditResult(
            audit_id="a1",
            intake_result=IntakeResult(success=True),
            verification_result=VerificationResult(success=True, verified_findings=sample_findings),
        ),
    )
    monkeypatch.setattr(
        ax,
        "run_report_packaging_phase",
        mock.AsyncMock(
            return_value=ReportPackagingResult(success=True, final_backlog=sample_findings)
        ),
    )
    result = asyncio.run(ax.run_report_packaging_step("j1", "a1"))
    assert result.report_packaging_result is not None
    assert Phase.REPORT_PACKAGING in result.completed_phases
    # Final assembly (success + counts) is deferred to finalize_audit_step.
    assert result.success is False
    assert result.total_findings == 0


def test_run_report_packaging_step_raises_when_state_missing(monkeypatch):
    monkeypatch.setattr(ax, "_load_audit_state_strict", mock.AsyncMock(return_value=None))
    with pytest.raises(RuntimeError, match="verification state"):
        asyncio.run(ax.run_report_packaging_step("j1", "a1"))


def test_run_report_packaging_step_skips_rerun_when_already_complete(monkeypatch, sample_findings):
    seeded = AccessibilityAuditResult(
        audit_id="a1",
        intake_result=IntakeResult(success=True),
        verification_result=VerificationResult(success=True, verified_findings=sample_findings),
        report_packaging_result=ReportPackagingResult(success=True, final_backlog=sample_findings),
        completed_phases=[
            Phase.INTAKE,
            Phase.DISCOVERY,
            Phase.VERIFICATION,
            Phase.REPORT_PACKAGING,
        ],
    )
    _seed(monkeypatch, seeded)
    phase_fn = mock.AsyncMock()
    monkeypatch.setattr(ax, "run_report_packaging_phase", phase_fn)

    result = asyncio.run(ax.run_report_packaging_step("j1", "a1"))

    phase_fn.assert_not_called()
    assert result is seeded


def test_run_discovery_step_is_idempotent_on_phase(monkeypatch, sample_audit_plan, sample_findings):
    """A Temporal at-least-once retry reloading a result whose completed_phases
    already contains DISCOVERY (the completion ack to the server was lost, so the
    activity re-runs after it already succeeded and persisted) must not duplicate
    the entry."""
    _seed(
        monkeypatch,
        AccessibilityAuditResult(
            audit_id="a1",
            intake_result=IntakeResult(success=True, audit_plan=sample_audit_plan),
            discovery_result=DiscoveryResult(success=True, draft_findings=sample_findings),
            completed_phases=[Phase.INTAKE, Phase.DISCOVERY],
        ),
    )
    monkeypatch.setattr(
        ax,
        "run_discovery_phase",
        mock.AsyncMock(return_value=DiscoveryResult(success=True, draft_findings=sample_findings)),
    )
    result = asyncio.run(ax.run_discovery_step("j1", "a1"))
    assert result.completed_phases.count(Phase.DISCOVERY) == 1


def test_run_discovery_step_skips_rerun_when_already_complete(
    monkeypatch, sample_audit_plan, sample_findings
):
    """The scans/LLM calls are nondeterministic and can create side-effecting
    artifacts, so a retry after DISCOVERY is already recorded complete must not
    re-run them at all — not just avoid duplicating the completed_phases entry."""
    seeded = AccessibilityAuditResult(
        audit_id="a1",
        intake_result=IntakeResult(success=True, audit_plan=sample_audit_plan),
        discovery_result=DiscoveryResult(success=True, draft_findings=sample_findings),
        completed_phases=[Phase.INTAKE, Phase.DISCOVERY],
    )
    _seed(monkeypatch, seeded)
    phase_fn = mock.AsyncMock()
    monkeypatch.setattr(ax, "run_discovery_phase", phase_fn)

    result = asyncio.run(ax.run_discovery_step("j1", "a1"))

    phase_fn.assert_not_called()
    assert result is seeded


def test_run_discovery_step_raises_when_persist_fails(monkeypatch, sample_audit_plan):
    """Persistence is load-bearing for a per-phase Temporal step (the only channel
    the next activity has to this state): a store write failure must fail this
    activity loudly rather than silently reporting PASS with unsaved state."""
    _seed(
        monkeypatch,
        AccessibilityAuditResult(
            audit_id="a1", intake_result=IntakeResult(success=True, audit_plan=sample_audit_plan)
        ),
    )
    monkeypatch.setattr(
        ax,
        "run_discovery_phase",
        mock.AsyncMock(return_value=DiscoveryResult(success=True)),
    )
    monkeypatch.setattr(ax, "persist_audit_state", mock.AsyncMock(return_value=False))
    with pytest.raises(RuntimeError, match="failed to persist audit state"):
        asyncio.run(ax.run_discovery_step("j1", "a1"))


def test_run_discovery_step_failure(monkeypatch, sample_audit_plan):
    _seed(
        monkeypatch,
        AccessibilityAuditResult(
            audit_id="a1", intake_result=IntakeResult(success=True, audit_plan=sample_audit_plan)
        ),
    )
    monkeypatch.setattr(
        ax,
        "run_discovery_phase",
        mock.AsyncMock(return_value=DiscoveryResult(success=False, error="discovery boom")),
    )
    result = asyncio.run(ax.run_discovery_step("j1", "a1"))
    assert result.failure_reason == "discovery boom"
    assert Phase.DISCOVERY not in result.completed_phases


def test_run_discovery_step_raises_when_persist_fails_on_logical_failure(
    monkeypatch, sample_audit_plan
):
    """A store-write failure while persisting a LOGICAL discovery failure must
    also fail this activity loudly (mirroring the success-path guard) rather than
    silently returning a failure result whose audit_state was never saved — a
    concurrent path could otherwise see a stale, not-actually-failed state."""
    _seed(
        monkeypatch,
        AccessibilityAuditResult(
            audit_id="a1", intake_result=IntakeResult(success=True, audit_plan=sample_audit_plan)
        ),
    )
    monkeypatch.setattr(
        ax,
        "run_discovery_phase",
        mock.AsyncMock(return_value=DiscoveryResult(success=False, error="discovery boom")),
    )
    monkeypatch.setattr(ax, "persist_audit_state", mock.AsyncMock(return_value=False))
    with pytest.raises(RuntimeError, match="failed to persist audit state"):
        asyncio.run(ax.run_discovery_step("j1", "a1"))


def test_run_verification_step_failure(monkeypatch, sample_findings):
    _seed(
        monkeypatch,
        AccessibilityAuditResult(
            audit_id="a1",
            discovery_result=DiscoveryResult(success=True, draft_findings=sample_findings),
        ),
    )
    monkeypatch.setattr(
        ax,
        "run_verification_phase",
        mock.AsyncMock(return_value=VerificationResult(success=False, error="verif boom")),
    )
    result = asyncio.run(ax.run_verification_step("j1", "a1", {}))
    assert result.failure_reason == "verif boom"
    assert Phase.VERIFICATION not in result.completed_phases


def test_run_verification_step_raises_when_persist_fails_on_logical_failure(
    monkeypatch, sample_findings
):
    _seed(
        monkeypatch,
        AccessibilityAuditResult(
            audit_id="a1",
            discovery_result=DiscoveryResult(success=True, draft_findings=sample_findings),
        ),
    )
    monkeypatch.setattr(
        ax,
        "run_verification_phase",
        mock.AsyncMock(return_value=VerificationResult(success=False, error="verif boom")),
    )
    monkeypatch.setattr(ax, "persist_audit_state", mock.AsyncMock(return_value=False))
    with pytest.raises(RuntimeError, match="failed to persist audit state"):
        asyncio.run(ax.run_verification_step("j1", "a1", {}))


def test_run_verification_step_raises_when_state_missing(monkeypatch):
    monkeypatch.setattr(ax, "_load_audit_state_strict", mock.AsyncMock(return_value=None))
    with pytest.raises(RuntimeError, match="discovery state"):
        asyncio.run(ax.run_verification_step("j1", "a1", {}))


def test_run_verification_step_is_idempotent_on_phase(monkeypatch, sample_findings):
    """Same idempotency contract as discovery: a retry reloading state that
    already has VERIFICATION recorded must not duplicate the entry."""
    _seed(
        monkeypatch,
        AccessibilityAuditResult(
            audit_id="a1",
            discovery_result=DiscoveryResult(success=True, draft_findings=sample_findings),
            verification_result=VerificationResult(success=True, verified_findings=sample_findings),
            completed_phases=[Phase.INTAKE, Phase.DISCOVERY, Phase.VERIFICATION],
        ),
    )
    monkeypatch.setattr(
        ax,
        "run_verification_phase",
        mock.AsyncMock(
            return_value=VerificationResult(success=True, verified_findings=sample_findings)
        ),
    )
    result = asyncio.run(ax.run_verification_step("j1", "a1", {}))
    assert result.completed_phases.count(Phase.VERIFICATION) == 1


def test_run_verification_step_skips_rerun_when_already_complete(monkeypatch, sample_findings):
    seeded = AccessibilityAuditResult(
        audit_id="a1",
        discovery_result=DiscoveryResult(success=True, draft_findings=sample_findings),
        verification_result=VerificationResult(success=True, verified_findings=sample_findings),
        completed_phases=[Phase.INTAKE, Phase.DISCOVERY, Phase.VERIFICATION],
    )
    _seed(monkeypatch, seeded)
    phase_fn = mock.AsyncMock()
    monkeypatch.setattr(ax, "run_verification_phase", phase_fn)

    result = asyncio.run(ax.run_verification_step("j1", "a1", {}))

    phase_fn.assert_not_called()
    assert result is seeded


def test_run_verification_step_raises_when_persist_fails(monkeypatch, sample_findings):
    _seed(
        monkeypatch,
        AccessibilityAuditResult(
            audit_id="a1",
            discovery_result=DiscoveryResult(success=True, draft_findings=sample_findings),
        ),
    )
    monkeypatch.setattr(
        ax,
        "run_verification_phase",
        mock.AsyncMock(
            return_value=VerificationResult(success=True, verified_findings=sample_findings)
        ),
    )
    monkeypatch.setattr(ax, "persist_audit_state", mock.AsyncMock(return_value=False))
    with pytest.raises(RuntimeError, match="failed to persist audit state"):
        asyncio.run(ax.run_verification_step("j1", "a1", {}))


def test_run_intake_step_raises_when_persist_fails(monkeypatch, sample_audit_plan):
    monkeypatch.setattr(
        ax,
        "run_intake_phase",
        mock.AsyncMock(return_value=IntakeResult(success=True, audit_plan=sample_audit_plan)),
    )
    monkeypatch.setattr(ax, "persist_audit_state", mock.AsyncMock(return_value=False))
    with pytest.raises(RuntimeError, match="failed to persist audit state"):
        asyncio.run(ax.run_intake_step("j1", "a1", ax.CreateAuditRequest(web_urls=[])))


def test_run_report_packaging_step_failure(monkeypatch, sample_findings):
    _seed(
        monkeypatch,
        AccessibilityAuditResult(
            audit_id="a1",
            intake_result=IntakeResult(success=True),
            verification_result=VerificationResult(success=True, verified_findings=sample_findings),
        ),
    )
    monkeypatch.setattr(
        ax,
        "run_report_packaging_phase",
        mock.AsyncMock(return_value=ReportPackagingResult(success=False, error="report boom")),
    )
    result = asyncio.run(ax.run_report_packaging_step("j1", "a1"))
    assert result.failure_reason == "report boom"
    assert Phase.REPORT_PACKAGING not in result.completed_phases


def test_run_report_packaging_step_raises_when_persist_fails_on_logical_failure(
    monkeypatch, sample_findings
):
    _seed(
        monkeypatch,
        AccessibilityAuditResult(
            audit_id="a1",
            intake_result=IntakeResult(success=True),
            verification_result=VerificationResult(success=True, verified_findings=sample_findings),
        ),
    )
    monkeypatch.setattr(
        ax,
        "run_report_packaging_phase",
        mock.AsyncMock(return_value=ReportPackagingResult(success=False, error="report boom")),
    )
    monkeypatch.setattr(ax, "persist_audit_state", mock.AsyncMock(return_value=False))
    with pytest.raises(RuntimeError, match="failed to persist audit state"):
        asyncio.run(ax.run_report_packaging_step("j1", "a1"))


def test_run_report_packaging_step_raises_when_persist_fails(monkeypatch, sample_findings):
    _seed(
        monkeypatch,
        AccessibilityAuditResult(
            audit_id="a1",
            intake_result=IntakeResult(success=True),
            verification_result=VerificationResult(success=True, verified_findings=sample_findings),
        ),
    )
    monkeypatch.setattr(
        ax,
        "run_report_packaging_phase",
        mock.AsyncMock(
            return_value=ReportPackagingResult(success=True, final_backlog=sample_findings)
        ),
    )
    monkeypatch.setattr(ax, "persist_audit_state", mock.AsyncMock(return_value=False))
    with pytest.raises(RuntimeError, match="failed to persist audit state"):
        asyncio.run(ax.run_report_packaging_step("j1", "a1"))


# ---------------------------------------------------------------------------
# finalize_audit_step
# ---------------------------------------------------------------------------


def test_finalize_audit_step_assembles_result(monkeypatch, sample_findings):
    _seed(
        monkeypatch,
        AccessibilityAuditResult(
            audit_id="a1",
            report_packaging_result=ReportPackagingResult(
                success=True, final_backlog=sample_findings
            ),
        ),
    )
    result = asyncio.run(ax.finalize_audit_step("j1", "a1"))
    assert result.success is True
    assert result.total_findings == 3


def test_finalize_audit_step_raises_when_state_missing(monkeypatch):
    monkeypatch.setattr(ax, "_load_audit_state_strict", mock.AsyncMock(return_value=None))
    with pytest.raises(RuntimeError, match="report-packaging state"):
        asyncio.run(ax.finalize_audit_step("j1", "a1"))


def test_finalize_audit_step_skips_rerun_when_already_finalized(monkeypatch, sample_findings):
    """A Temporal retry after finalize already succeeded and persisted (the
    completion ack was lost) must not redo the severity-count/summary assembly
    or re-persist — finalize_audit_result is deterministic but the extra work
    and store write are unnecessary."""
    seeded = AccessibilityAuditResult(
        audit_id="a1",
        success=True,
        total_findings=3,
        summary="Audit complete. 3 findings (1 critical, 1 high, 1 medium, 0 low). 0 patterns identified.",
        report_packaging_result=ReportPackagingResult(success=True, final_backlog=sample_findings),
        completed_phases=[Phase.REPORT_PACKAGING],
    )
    _seed(monkeypatch, seeded)
    persist = mock.AsyncMock(return_value=True)
    monkeypatch.setattr(ax, "persist_audit_state", persist)

    result = asyncio.run(ax.finalize_audit_step("j1", "a1"))

    assert result is seeded
    persist.assert_not_awaited()


def test_finalize_audit_step_raises_when_report_unsuccessful(monkeypatch):
    """finalize_audit_result's precondition (report succeeded) is enforced — an
    unsuccessful report reaching finalize is a plumbing defect that fails loudly."""
    _seed(
        monkeypatch,
        AccessibilityAuditResult(
            audit_id="a1",
            report_packaging_result=ReportPackagingResult(success=False, error="nope"),
        ),
    )
    with pytest.raises(RuntimeError, match="did not succeed"):
        asyncio.run(ax.finalize_audit_step("j1", "a1"))


def test_finalize_audit_step_raises_when_persist_fails(monkeypatch, sample_findings):
    _seed(
        monkeypatch,
        AccessibilityAuditResult(
            audit_id="a1",
            report_packaging_result=ReportPackagingResult(
                success=True, final_backlog=sample_findings
            ),
        ),
    )
    monkeypatch.setattr(ax, "persist_audit_state", mock.AsyncMock(return_value=False))
    with pytest.raises(RuntimeError, match="failed to persist audit state"):
        asyncio.run(ax.finalize_audit_step("j1", "a1"))


# ---------------------------------------------------------------------------
# _persist_unless_job_terminal — guards a step's own persist against clobbering
# a job a concurrent path (e.g. a timebox timeout) already marked terminal
# ---------------------------------------------------------------------------


def test_persist_unless_job_terminal_persists_when_job_not_terminal(monkeypatch):
    persist = mock.AsyncMock(return_value=True)
    monkeypatch.setattr(ax, "persist_audit_state", persist)
    jm = mock.Mock()
    jm.get_job.return_value = {"status": ax.JOB_STATUS_RUNNING}
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)
    result = AccessibilityAuditResult(audit_id="a1")

    ok = asyncio.run(ax._persist_unless_job_terminal("j1", result))

    assert ok is True
    persist.assert_awaited_once_with(result)


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
def test_persist_unless_job_terminal_skips_when_job_terminal(monkeypatch, terminal_status):
    """A concurrent path (e.g. mark_timed_out_activity) already decided this job's
    outcome — the skip must report success (True), not a persistence failure, so
    the caller doesn't spuriously raise RuntimeError over an abandoned write."""
    persist = mock.AsyncMock(return_value=True)
    monkeypatch.setattr(ax, "persist_audit_state", persist)
    jm = mock.Mock()
    jm.get_job.return_value = {"status": terminal_status}
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)
    result = AccessibilityAuditResult(audit_id="a1")

    ok = asyncio.run(ax._persist_unless_job_terminal("j1", result))

    assert ok is True
    persist.assert_not_awaited()


def test_persist_unless_job_terminal_persists_when_job_missing(monkeypatch):
    """No job row (e.g. called outside the normal job lifecycle) is not terminal —
    the persist still goes through."""
    persist = mock.AsyncMock(return_value=True)
    monkeypatch.setattr(ax, "persist_audit_state", persist)
    jm = mock.Mock()
    jm.get_job.return_value = None
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)
    result = AccessibilityAuditResult(audit_id="a1")

    ok = asyncio.run(ax._persist_unless_job_terminal("j1", result))

    assert ok is True
    persist.assert_awaited_once_with(result)


def test_run_discovery_step_skips_persist_when_job_already_terminal(monkeypatch, sample_audit_plan):
    """A cancelled-but-still-running discovery step (e.g. abandoned after a
    timebox timeout already marked the job FAILED) must not clobber the
    already-decided terminal audit_state with its own late write."""
    _seed(
        monkeypatch,
        AccessibilityAuditResult(
            audit_id="a1", intake_result=IntakeResult(success=True, audit_plan=sample_audit_plan)
        ),
    )
    monkeypatch.setattr(
        ax, "run_discovery_phase", mock.AsyncMock(return_value=DiscoveryResult(success=True))
    )
    persist = mock.AsyncMock(return_value=True)
    monkeypatch.setattr(ax, "persist_audit_state", persist)
    jm = mock.Mock()
    jm.get_job.return_value = {"status": ax.JOB_STATUS_FAILED}
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)

    result = asyncio.run(ax.run_discovery_step("j1", "a1"))

    persist.assert_not_awaited()
    assert Phase.DISCOVERY in result.completed_phases  # in-memory result still assembled


# ---------------------------------------------------------------------------
# persist_audit_state / load_audit_state round-trip (real filesystem backend)
# ---------------------------------------------------------------------------


def test_persist_and_load_audit_state_round_trip():
    """The store (redirected to a tmp dir by the autouse fixture) round-trips a result."""
    result = AccessibilityAuditResult(audit_id="rt_unique", success=True, total_findings=2)
    assert asyncio.run(ax.persist_audit_state(result)) is True
    loaded = asyncio.run(ax.load_audit_state("rt_unique"))
    assert loaded is not None
    assert loaded.audit_id == "rt_unique"
    assert loaded.success is True
    assert loaded.total_findings == 2


def test_load_audit_state_returns_none_when_absent():
    """A missing ref loads to None (not an error)."""
    assert asyncio.run(ax.load_audit_state("does_not_exist")) is None


def test_mark_audit_timed_out_marks_job_and_state(monkeypatch):
    """The timeout helper records the timebox reason on both the job and the
    persisted audit state, listing the phases that did complete."""
    import json

    seeded = AccessibilityAuditResult(audit_id="a1", completed_phases=[Phase.INTAKE])
    monkeypatch.setattr(ax, "_load_audit_state_strict", mock.AsyncMock(return_value=seeded))
    jm = mock.Mock()
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)

    asyncio.run(ax.mark_audit_timed_out("j1", "a1", 2))

    failed = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == ax.JOB_STATUS_FAILED
    ]
    assert failed
    error = failed[0].kwargs["error"]
    assert "timed out after 2 hour" in error
    assert "intake" in error
    assert seeded.success is False
    # started_at is a raw datetime by default; the terminal write must use a
    # JSON-mode dump so this stays plain JSON-serializable data.
    result_dict = failed[0].kwargs["result"]
    json.dumps(result_dict)
    assert isinstance(result_dict["started_at"], str)


def test_mark_audit_timed_out_without_persisted_state(monkeypatch):
    """With no persisted state, the job is still marked failed with a timeout reason."""
    monkeypatch.setattr(ax, "_load_audit_state_strict", mock.AsyncMock(return_value=None))
    jm = mock.Mock()
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)

    asyncio.run(ax.mark_audit_timed_out("j1", "a1", 1))

    failed = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == ax.JOB_STATUS_FAILED
    ]
    assert failed and "timed out after 1 hour" in failed[0].kwargs["error"]


def test_mark_audit_timed_out_raises_when_persist_fails(monkeypatch):
    """A transient artifact-store failure while persisting the flipped timeout
    state must still mark the job failed (best-effort, so a client polling job
    status isn't left hanging) but also raise so Temporal retries this activity —
    otherwise audit_state_{audit_id} stays stale (not reflecting the timeout) for
    any /report or /findings reader, with no retry to fix it."""
    seeded = AccessibilityAuditResult(audit_id="a1", completed_phases=[Phase.INTAKE])
    monkeypatch.setattr(ax, "_load_audit_state_strict", mock.AsyncMock(return_value=seeded))
    monkeypatch.setattr(ax, "persist_audit_state", mock.AsyncMock(return_value=False))
    jm = mock.Mock()
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)

    with pytest.raises(RuntimeError, match="failed to persist audit state"):
        asyncio.run(ax.mark_audit_timed_out("j1", "a1", 2))

    # The job is still marked failed despite the persist failure.
    failed = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == ax.JOB_STATUS_FAILED
    ]
    assert failed


def test_mark_audit_timed_out_raises_when_read_fails(monkeypatch):
    """A transient artifact-store READ failure (as opposed to a clean "nothing
    was ever persisted" miss) must be distinguished by the strict loader: the job
    is still marked failed (best-effort, with empty/default recovered fields),
    but the activity raises so Temporal retries — otherwise a stale, unmarked
    audit_state_{audit_id} left over from before the timeout is never fixed."""
    monkeypatch.setattr(
        ax, "_load_audit_state_strict", mock.AsyncMock(side_effect=RuntimeError("store down"))
    )
    persist = mock.AsyncMock()
    monkeypatch.setattr(ax, "persist_audit_state", persist)
    jm = mock.Mock()
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)

    with pytest.raises(RuntimeError, match="failed to persist audit state"):
        asyncio.run(ax.mark_audit_timed_out("j1", "a1", 3))

    persist.assert_not_awaited()  # nothing to persist — the read itself failed
    failed = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == ax.JOB_STATUS_FAILED
    ]
    assert failed and "timed out after 3 hour" in failed[0].kwargs["error"]


def test_mark_audit_timed_out_does_not_raise_on_clean_miss(monkeypatch):
    """A genuine miss (nothing was ever persisted before the timeout, e.g. intake
    itself never completed) must NOT be mistaken for a read failure — no raise,
    matching the existing test_mark_audit_timed_out_without_persisted_state
    behavior, now via the strict loader."""
    monkeypatch.setattr(ax, "_load_audit_state_strict", mock.AsyncMock(return_value=None))
    jm = mock.Mock()
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)

    asyncio.run(ax.mark_audit_timed_out("j1", "a1", 1))  # must not raise

    failed = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == ax.JOB_STATUS_FAILED
    ]
    assert failed and "timed out after 1 hour" in failed[0].kwargs["error"]


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
def test_mark_audit_timed_out_skips_when_job_already_terminal(monkeypatch, terminal_status):
    """The workflow's timebox timer and its phase chain genuinely race: the
    phase chain can finish and persist a result microseconds before the timer
    fires, so by the time this activity runs the job may already be terminal.
    Overwriting a genuine outcome (success or an unrelated failure) with a
    spurious 'timed out' one would be worse than a no-op here."""
    load = mock.AsyncMock()
    monkeypatch.setattr(ax, "_load_audit_state_strict", load)
    jm = mock.Mock()
    jm.get_job.return_value = {"status": terminal_status}
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)

    asyncio.run(ax.mark_audit_timed_out("j1", "a1", 2))  # must not raise

    load.assert_not_awaited()
    jm.update_job.assert_not_called()


def test_mark_audit_timed_out_skips_when_persisted_state_already_terminal(monkeypatch):
    """Even when the job-store write hasn't landed yet (job still shows
    RUNNING), a persisted audit state that already reached a terminal outcome
    (finalize_audit_step's persist landed before this read) must not be
    overwritten with a timeout — the race window this closes is between that
    persist and finalize_activity's own job-store write."""
    seeded = AccessibilityAuditResult(audit_id="a1", success=True, total_findings=3)
    monkeypatch.setattr(ax, "_load_audit_state_strict", mock.AsyncMock(return_value=seeded))
    persist = mock.AsyncMock()
    monkeypatch.setattr(ax, "persist_audit_state", persist)
    jm = mock.Mock()
    jm.get_job.return_value = {"status": ax.JOB_STATUS_RUNNING}
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)

    asyncio.run(ax.mark_audit_timed_out("j1", "a1", 2))  # must not raise

    persist.assert_not_awaited()
    jm.update_job.assert_not_called()
    # The genuinely-successful result must not be flipped to a failure.
    assert seeded.success is True
    assert seeded.failure_reason == ""


def test_mark_audit_timed_out_skips_when_persisted_failure_already_terminal(monkeypatch):
    """Same guard, other terminal branch: a persisted state that already failed
    for a different, genuine reason must not have that reason overwritten with
    a misleading 'timed out' one."""
    seeded = AccessibilityAuditResult(
        audit_id="a1", success=False, failure_reason="Discovery failed: scan crashed"
    )
    monkeypatch.setattr(ax, "_load_audit_state_strict", mock.AsyncMock(return_value=seeded))
    persist = mock.AsyncMock()
    monkeypatch.setattr(ax, "persist_audit_state", persist)
    jm = mock.Mock()
    jm.get_job.return_value = {"status": ax.JOB_STATUS_RUNNING}
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)

    asyncio.run(ax.mark_audit_timed_out("j1", "a1", 2))  # must not raise

    persist.assert_not_awaited()
    jm.update_job.assert_not_called()
    assert seeded.failure_reason == "Discovery failed: scan crashed"


def test_load_audit_state_strict_round_trips_like_load(monkeypatch):
    """On a clean hit, the strict loader returns the same state as the swallowing one."""
    result = AccessibilityAuditResult(audit_id="strict_rt", success=True, total_findings=1)
    asyncio.run(ax.persist_audit_state(result))
    loaded = asyncio.run(ax._load_audit_state_strict("strict_rt"))
    assert loaded is not None
    assert loaded.audit_id == "strict_rt"


def test_load_audit_state_strict_returns_none_when_absent():
    """A genuine store miss (no ref under this key) is a clean None, not an error."""
    assert asyncio.run(ax._load_audit_state_strict("does_not_exist")) is None


def test_load_audit_state_strict_propagates_store_errors(monkeypatch):
    """Unlike load_audit_state, a transient store error must propagate so the
    caller (run_intake_step's idempotency check) fails loudly and lets Temporal
    retry the read, instead of silently treating it as 'no prior state'."""
    import accessibility_audit_team.artifact_store as store_mod

    def _boom():
        raise RuntimeError("store down")

    monkeypatch.setattr(store_mod, "get_artifact_store", _boom)
    with pytest.raises(RuntimeError, match="store down"):
        asyncio.run(ax._load_audit_state_strict("a1"))


def test_run_intake_step_skips_rerun_when_already_failed(monkeypatch):
    """A retry after intake already failed AND persisted (the job-store FAILED
    write for that failure itself failed, so Temporal retries this activity) must
    not re-run the nondeterministic intake LLM call and overwrite the original
    failure with a different one."""
    seeded = AccessibilityAuditResult(audit_id="a1", success=False, failure_reason="original boom")
    asyncio.run(ax.persist_audit_state(seeded))
    phase_fn = mock.AsyncMock()
    monkeypatch.setattr(ax, "run_intake_phase", phase_fn)

    result = asyncio.run(ax.run_intake_step("j1", "a1", ax.CreateAuditRequest(web_urls=[])))

    phase_fn.assert_not_called()
    assert result.failure_reason == "original boom"


def test_run_discovery_step_skips_rerun_when_already_failed(monkeypatch, sample_audit_plan):
    seeded = AccessibilityAuditResult(
        audit_id="a1",
        intake_result=IntakeResult(success=True, audit_plan=sample_audit_plan),
        success=False,
        failure_reason="original discovery boom",
    )
    _seed(monkeypatch, seeded)
    phase_fn = mock.AsyncMock()
    monkeypatch.setattr(ax, "run_discovery_phase", phase_fn)

    result = asyncio.run(ax.run_discovery_step("j1", "a1"))

    phase_fn.assert_not_called()
    assert result is seeded


def test_run_verification_step_skips_rerun_when_already_failed(monkeypatch, sample_findings):
    seeded = AccessibilityAuditResult(
        audit_id="a1",
        discovery_result=DiscoveryResult(success=True, draft_findings=sample_findings),
        success=False,
        failure_reason="original verification boom",
    )
    _seed(monkeypatch, seeded)
    phase_fn = mock.AsyncMock()
    monkeypatch.setattr(ax, "run_verification_phase", phase_fn)

    result = asyncio.run(ax.run_verification_step("j1", "a1", {}))

    phase_fn.assert_not_called()
    assert result is seeded


def test_run_report_packaging_step_skips_rerun_when_already_failed(monkeypatch, sample_findings):
    seeded = AccessibilityAuditResult(
        audit_id="a1",
        verification_result=VerificationResult(success=True, verified_findings=sample_findings),
        success=False,
        failure_reason="original report boom",
    )
    _seed(monkeypatch, seeded)
    phase_fn = mock.AsyncMock()
    monkeypatch.setattr(ax, "run_report_packaging_phase", phase_fn)

    result = asyncio.run(ax.run_report_packaging_step("j1", "a1"))

    phase_fn.assert_not_called()
    assert result is seeded


def test_persist_and_load_swallow_store_errors(monkeypatch):
    """A store failure is logged and swallowed (never raised) by both helpers
    (best-effort recovery); ``persist_audit_state`` reports the failure to its
    caller via a ``False`` return rather than an exception, so a caller for whom
    the write is load-bearing (a per-phase step) can check it and fail loudly
    itself."""
    import accessibility_audit_team.artifact_store as store_mod

    def _boom():
        raise RuntimeError("store down")

    monkeypatch.setattr(store_mod, "get_artifact_store", _boom)
    # Neither raises despite the store being unavailable.
    assert asyncio.run(ax.persist_audit_state(AccessibilityAuditResult(audit_id="a1"))) is False
    assert asyncio.run(ax.load_audit_state("a1")) is None
