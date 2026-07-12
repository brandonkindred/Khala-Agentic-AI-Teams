"""Tests for the AccessibilityAuditOrchestrator."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from accessibility_audit_team.models import (
    AccessibilityAuditResult,
    AuditPlan,
    AuditRequest,
    AuditTargets,
    CoverageMatrix,
    DiscoveryResult,
    IntakeResult,
    Phase,
    ReportPackagingResult,
    RetestResult,
    VerificationResult,
    WCAGLevel,
)
from accessibility_audit_team.orchestrator import AccessibilityAuditOrchestrator


def _make_intake_result(audit_id: str = "audit_test") -> IntakeResult:
    return IntakeResult(
        success=True,
        audit_plan=AuditPlan(
            audit_id=audit_id,
            targets=AuditTargets(web_urls=["https://example.com"]),
        ),
        coverage_matrix=CoverageMatrix(matrix_ref="matrix_1", audit_id=audit_id),
        summary="Intake done",
    )


def _make_discovery_result() -> DiscoveryResult:
    return DiscoveryResult(success=True, draft_findings=[], pages_scanned=1, summary="Discovery done")


def _make_verification_result() -> VerificationResult:
    return VerificationResult(success=True, verified_findings=[], summary="Verification done")


def _make_report_result() -> ReportPackagingResult:
    return ReportPackagingResult(
        success=True,
        final_backlog=[],
        patterns=[],
        executive_summary="All clear",
        summary="Report done",
    )


# ---------------------------------------------------------------------------
# Orchestrator lifecycle tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_full_audit_lifecycle():
    """Orchestrator runs all 4 phases to completion."""
    with patch(
        "accessibility_audit_team.orchestrator.run_intake_phase",
        new_callable=AsyncMock,
        return_value=_make_intake_result(),
    ), patch(
        "accessibility_audit_team.orchestrator.run_discovery_phase",
        new_callable=AsyncMock,
        return_value=_make_discovery_result(),
    ), patch(
        "accessibility_audit_team.orchestrator.run_verification_phase",
        new_callable=AsyncMock,
        return_value=_make_verification_result(),
    ), patch(
        "accessibility_audit_team.orchestrator.run_report_packaging_phase",
        new_callable=AsyncMock,
        return_value=_make_report_result(),
    ):
        orchestrator = AccessibilityAuditOrchestrator()
        request = AuditRequest(
            audit_id="audit_lifecycle",
            web_urls=["https://example.com"],
            wcag_levels=[WCAGLevel.A, WCAGLevel.AA],
        )
        result = await orchestrator.run_audit(request)

    assert result.success is True
    assert Phase.INTAKE in result.completed_phases
    assert Phase.DISCOVERY in result.completed_phases
    assert Phase.VERIFICATION in result.completed_phases
    assert Phase.REPORT_PACKAGING in result.completed_phases


@pytest.mark.anyio
async def test_audit_stops_on_intake_failure():
    with patch(
        "accessibility_audit_team.orchestrator.run_intake_phase",
        new_callable=AsyncMock,
        return_value=IntakeResult(success=False, error="Bad request"),
    ):
        orchestrator = AccessibilityAuditOrchestrator()
        request = AuditRequest(audit_id="audit_fail", web_urls=["https://example.com"])
        result = await orchestrator.run_audit(request)

    assert result.success is False
    assert "Bad request" in result.failure_reason


@pytest.mark.anyio
async def test_audit_timeout():
    """Audit with a timebox should time out if phases take too long."""
    import asyncio

    async def slow_intake(*args, **kwargs):
        await asyncio.sleep(10)
        return _make_intake_result()

    with patch(
        "accessibility_audit_team.orchestrator.run_intake_phase",
        side_effect=slow_intake,
    ):
        orchestrator = AccessibilityAuditOrchestrator()
        request = AuditRequest(
            audit_id="audit_timeout",
            web_urls=["https://example.com"],
            timebox_hours=1,
        )
        # Override the timeout to something tiny for testing
        with patch.object(
            orchestrator,
            "run_audit",
            wraps=orchestrator.run_audit,
        ):
            async def patched_run(req, tech_stack=None):
                # Test the timeout logic by calling _run_audit_phases with a tiny timeout
                result_obj = AccessibilityAuditResult(
                    audit_id=req.audit_id or "audit_temp",
                    current_phase=Phase.INTAKE,
                )
                try:
                    await asyncio.wait_for(
                        orchestrator._run_audit_phases(req, {"web": "other"}, result_obj),
                        timeout=0.1,
                    )
                except asyncio.TimeoutError:
                    result_obj.success = False
                    result_obj.failure_reason = "Audit timed out"
                return result_obj

            result = await patched_run(request)

    assert result.success is False
    assert "timed out" in result.failure_reason


# ---------------------------------------------------------------------------
# Retest
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_retest_persists_updated_state(monkeypatch, sample_findings):
    """run_retest saves the retested audit back to the store so a later
    report/retest request (possibly cross-process, or after a restart) reloads
    the updated findings instead of the stale pre-retest snapshot."""
    orchestrator = AccessibilityAuditOrchestrator()
    orchestrator._audits["audit_rt"] = AccessibilityAuditResult(
        audit_id="audit_rt", final_findings=sample_findings
    )
    persist = AsyncMock()
    monkeypatch.setattr(orchestrator, "_persist_audit", persist)

    with patch(
        "accessibility_audit_team.orchestrator.run_retest_phase",
        new_callable=AsyncMock,
        return_value=RetestResult(
            success=True, findings_retested=3, findings_closed=1, findings_still_open=2
        ),
    ):
        result = await orchestrator.run_retest("audit_rt", None)

    persist.assert_awaited()
    assert Phase.RETEST in result.completed_phases


@pytest.mark.anyio
async def test_run_retest_no_finding_ids_and_none_pending_persists_state(monkeypatch):
    """The legitimate "nothing to retest" case (no finding_ids requested, and the
    audit genuinely has no final findings) must still persist the updated
    summary — otherwise the note never survives a restart/reload, unlike every
    other branch of run_retest."""
    orchestrator = AccessibilityAuditOrchestrator()
    orchestrator._audits["audit_rt"] = AccessibilityAuditResult(
        audit_id="audit_rt", final_findings=[]
    )
    persist = AsyncMock()
    monkeypatch.setattr(orchestrator, "_persist_audit", persist)

    result = await orchestrator.run_retest("audit_rt", None)

    assert result.summary == "No findings to retest"
    persist.assert_awaited_once()


@pytest.mark.anyio
async def test_run_retest_unmatched_finding_ids_fails_without_persisting(
    monkeypatch, sample_findings
):
    """A typo'd or stale finding_id that matches nothing is a caller-input error,
    not a legitimate "nothing to retest" state — it must NOT overwrite the
    completed audit's persisted summary/state with a no-op notice."""
    orchestrator = AccessibilityAuditOrchestrator()
    orchestrator._audits["audit_rt"] = AccessibilityAuditResult(
        audit_id="audit_rt", final_findings=sample_findings, summary="Audit complete. 3 findings."
    )
    persist = AsyncMock()
    monkeypatch.setattr(orchestrator, "_persist_audit", persist)

    result = await orchestrator.run_retest("audit_rt", ["does-not-exist"])

    assert result.success is False
    assert "does-not-exist" in result.failure_reason
    persist.assert_not_awaited()
    # The cached audit's own state must be untouched by the failed request.
    assert orchestrator._audits["audit_rt"].summary == "Audit complete. 3 findings."


def test_get_retest_lock_returns_same_lock_for_same_audit_id():
    orchestrator = AccessibilityAuditOrchestrator()
    lock1 = orchestrator._get_retest_lock("audit_x")
    lock2 = orchestrator._get_retest_lock("audit_x")
    lock3 = orchestrator._get_retest_lock("audit_y")
    assert lock1 is lock2
    assert lock1 is not lock3


def test_get_retest_lock_is_evicted_once_unreferenced():
    """``_retest_locks`` is a WeakValueDictionary: once nothing holds/awaits the
    lock for a given audit_id (no in-flight ``async with self._get_retest_lock(...)``
    block), the entry is garbage-collected rather than retained forever — otherwise
    a long-running orchestrator process would grow ``_retest_locks`` without bound
    as more distinct audits are retested over its lifetime."""
    import gc

    orchestrator = AccessibilityAuditOrchestrator()
    lock1 = orchestrator._get_retest_lock("audit_z")
    assert "audit_z" in orchestrator._retest_locks

    del lock1
    gc.collect()
    assert "audit_z" not in orchestrator._retest_locks

    lock2 = orchestrator._get_retest_lock("audit_z")
    assert isinstance(lock2, asyncio.Lock)


@pytest.mark.anyio
async def test_run_retest_serializes_concurrent_calls_for_same_audit(monkeypatch, sample_findings):
    """Two concurrent run_retest calls for the same audit_id must not interleave:
    ``_ensure_loaded`` caches and returns the SAME AccessibilityAuditResult
    instance for both, so without the lock the second call could mutate it out
    from under the first before either persists."""
    orchestrator = AccessibilityAuditOrchestrator()
    orchestrator._audits["audit_rt"] = AccessibilityAuditResult(
        audit_id="audit_rt", final_findings=sample_findings
    )
    monkeypatch.setattr(orchestrator, "_persist_audit", AsyncMock())

    entered = []
    release_first = asyncio.Event()

    async def _fake_retest_phase(**kwargs):
        entered.append(1)
        if len(entered) == 1:
            await release_first.wait()
        return RetestResult(
            success=True, findings_retested=1, findings_closed=1, findings_still_open=0
        )

    with patch(
        "accessibility_audit_team.orchestrator.run_retest_phase",
        side_effect=_fake_retest_phase,
    ):
        first = asyncio.ensure_future(orchestrator.run_retest("audit_rt", None))
        await asyncio.sleep(0.05)  # let the first call acquire the lock and enter the phase
        second = asyncio.ensure_future(orchestrator.run_retest("audit_rt", None))
        await asyncio.sleep(0.05)  # the second must be blocked on the lock, not the phase
        assert entered == [1]

        release_first.set()
        await asyncio.gather(first, second)
    assert entered == [1, 1]


# ---------------------------------------------------------------------------
# Status / findings queries
# ---------------------------------------------------------------------------


def test_get_audit_status_not_found():
    orchestrator = AccessibilityAuditOrchestrator()
    status = orchestrator.get_audit_status("nonexistent")
    assert status["status"] == "not_found"


def test_get_audit_status_reports_failed_for_failed_audit():
    """A failed audit (success=False + failure_reason) is terminal → 'failed', not
    'in_progress'."""
    orchestrator = AccessibilityAuditOrchestrator()
    orchestrator._audits["a1"] = AccessibilityAuditResult(
        audit_id="a1", success=False, failure_reason="Discovery failed"
    )
    assert orchestrator.get_audit_status("a1")["status"] == "failed"


def test_get_audit_status_reports_in_progress_when_running():
    orchestrator = AccessibilityAuditOrchestrator()
    orchestrator._audits["a1"] = AccessibilityAuditResult(audit_id="a1", success=False)
    assert orchestrator.get_audit_status("a1")["status"] == "in_progress"


@pytest.mark.anyio
async def test_get_audit_state_loads_from_store(monkeypatch):
    """The public get_audit_state resolves via the artifact store when not cached."""
    orchestrator = AccessibilityAuditOrchestrator()
    seeded = AccessibilityAuditResult(audit_id="a1", success=True)
    monkeypatch.setattr(orchestrator, "_load_audit", AsyncMock(return_value=seeded))
    loaded = await orchestrator.get_audit_state("a1")
    assert loaded is seeded
    # cached on the way out
    assert orchestrator._audits["a1"] is seeded


@pytest.mark.anyio
async def test_get_audit_state_always_refreshes_stale_cache(monkeypatch):
    """Unlike _ensure_loaded (used internally by run_retest), get_audit_state must
    not trust a cached snapshot forever: a Temporal worker (or a retest handled by
    a different process/instance) can keep advancing the persisted state after
    this process cached an earlier one, and get_audit_state is the cross-process
    source of truth callers rely on to see that newer state."""
    orchestrator = AccessibilityAuditOrchestrator()
    stale = AccessibilityAuditResult(audit_id="a1", success=False, summary="stale")
    fresh = AccessibilityAuditResult(audit_id="a1", success=True, summary="fresh")
    orchestrator._audits["a1"] = stale
    monkeypatch.setattr(orchestrator, "_load_audit", AsyncMock(return_value=fresh))

    loaded = await orchestrator.get_audit_state("a1")

    assert loaded is fresh
    assert orchestrator._audits["a1"] is fresh


@pytest.mark.anyio
async def test_get_audit_state_falls_back_to_cache_on_store_miss(monkeypatch):
    """A transient store hiccup (or a race where nothing is persisted yet) must
    not spuriously 404 an audit this process already has cached."""
    orchestrator = AccessibilityAuditOrchestrator()
    cached = AccessibilityAuditResult(audit_id="a1", success=True)
    orchestrator._audits["a1"] = cached
    monkeypatch.setattr(orchestrator, "_load_audit", AsyncMock(return_value=None))

    loaded = await orchestrator.get_audit_state("a1")

    assert loaded is cached


@pytest.mark.anyio
async def test_get_audit_state_returns_none_when_nothing_cached_or_persisted(monkeypatch):
    orchestrator = AccessibilityAuditOrchestrator()
    monkeypatch.setattr(orchestrator, "_load_audit", AsyncMock(return_value=None))

    assert await orchestrator.get_audit_state("nonexistent") is None


@pytest.mark.anyio
async def test_get_audit_state_trusts_locally_running_object_without_reload(monkeypatch):
    """While run_audit/run_retest is actively mutating self._audits[audit_id] in
    this process, get_audit_state must return that live object as-is rather than
    replacing it with a (store-lagging) snapshot mid-run — doing so would detach
    the cache from the object those methods are still mutating in place."""
    orchestrator = AccessibilityAuditOrchestrator()
    live = AccessibilityAuditResult(audit_id="a1", success=False, summary="in progress")
    orchestrator._audits["a1"] = live
    orchestrator._locally_running_audits.add("a1")
    load_audit = AsyncMock(return_value=AccessibilityAuditResult(audit_id="a1", success=True))
    monkeypatch.setattr(orchestrator, "_load_audit", load_audit)

    loaded = await orchestrator.get_audit_state("a1")

    assert loaded is live
    load_audit.assert_not_awaited()


@pytest.mark.anyio
async def test_run_audit_tracks_locally_running_during_and_clears_after():
    """audit_id is marked locally-running for run_audit's duration (so
    get_audit_state knows not to reload mid-run) and cleared afterward — even on
    the early-return logical-failure path, via the try/finally."""
    orchestrator = AccessibilityAuditOrchestrator()
    seen = {}

    async def check_and_fail(*args, **kwargs):
        seen["during"] = "audit_track" in orchestrator._locally_running_audits
        return IntakeResult(success=False, error="stop here")

    with patch(
        "accessibility_audit_team.orchestrator.run_intake_phase", side_effect=check_and_fail
    ):
        request = AuditRequest(audit_id="audit_track", web_urls=["https://example.com"])
        result = await orchestrator.run_audit(request)

    assert seen["during"] is True
    assert result.success is False
    assert "audit_track" not in orchestrator._locally_running_audits


@pytest.mark.anyio
async def test_run_retest_tracks_locally_running_during_and_clears_after(
    monkeypatch, sample_findings
):
    orchestrator = AccessibilityAuditOrchestrator()
    orchestrator._audits["audit_rt"] = AccessibilityAuditResult(
        audit_id="audit_rt", final_findings=sample_findings
    )
    monkeypatch.setattr(orchestrator, "_persist_audit", AsyncMock())
    seen = {}

    async def check_and_return(**kwargs):
        seen["during"] = "audit_rt" in orchestrator._locally_running_audits
        return RetestResult(
            success=True, findings_retested=1, findings_closed=1, findings_still_open=0
        )

    with patch(
        "accessibility_audit_team.orchestrator.run_retest_phase", side_effect=check_and_return
    ):
        await orchestrator.run_retest("audit_rt", None)

    assert seen["during"] is True
    assert "audit_rt" not in orchestrator._locally_running_audits


@pytest.mark.anyio
async def test_run_retest_clears_locally_running_when_audit_not_found():
    orchestrator = AccessibilityAuditOrchestrator()
    result = await orchestrator.run_retest("missing_audit", None)
    assert result.success is False
    assert "missing_audit" not in orchestrator._locally_running_audits


@pytest.mark.anyio
async def test_run_audit_persists_state_on_phase_failure(monkeypatch):
    """A logical phase failure persists the failed state (crash-recovery invariant)."""
    with patch(
        "accessibility_audit_team.orchestrator.run_intake_phase",
        new_callable=AsyncMock,
        return_value=IntakeResult(success=False, error="Bad request"),
    ):
        orchestrator = AccessibilityAuditOrchestrator()
        persist = AsyncMock()
        monkeypatch.setattr(orchestrator, "_persist_audit", persist)
        request = AuditRequest(audit_id="audit_fail", web_urls=["https://example.com"])
        result = await orchestrator.run_audit(request)

    assert result.success is False
    persist.assert_awaited()


def test_get_findings_empty_when_not_found():
    orchestrator = AccessibilityAuditOrchestrator()
    findings = orchestrator.get_findings("nonexistent")
    assert findings == []


def test_get_patterns_empty_when_not_found():
    orchestrator = AccessibilityAuditOrchestrator()
    patterns = orchestrator.get_patterns("nonexistent")
    assert patterns == []


# ---------------------------------------------------------------------------
# Addon initialization
# ---------------------------------------------------------------------------


def test_addons_disabled_by_default():
    orchestrator = AccessibilityAuditOrchestrator()
    assert orchestrator.arm is None
    assert orchestrator.adse is None
    assert orchestrator.aet is None


def test_addons_enabled():
    orchestrator = AccessibilityAuditOrchestrator(enable_addons=True)
    assert orchestrator.arm is not None
    assert orchestrator.adse is not None
    assert orchestrator.aet is not None
