"""Tests for ``SalesWorkflow`` orchestration.

The workflow body is exercised directly with ``workflow.execute_activity``
monkeypatched to a recorder that returns canned activity results. This asserts
the deterministic control flow: stage ordering, per-prospect fan-out, the exact
per-prospect argument pairing (dossier/qual/proposal → the right prospect),
entry-stage gating, advance/nurture routing, the no-prospects early-exit, the
``stopped`` short-circuit, per-item failure drop, dossier re-load at proposal,
the catch-all FAILED write, and per-activity retry policies.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from temporalio.exceptions import ActivityError, CancelledError

from sales_team.temporal import activities as acts
from sales_team.temporal import workflows as wf

_REQUEST = {
    "product_name": "ProductX",
    "value_proposition": "Save 20% on outbound time",
    "icp": {"industry": ["SaaS"]},
}

_PER_PROSPECT = {
    acts.outreach_one_activity,
    acts.qualify_one_activity,
    acts.nurture_one_activity,
    acts.discovery_one_activity,
    acts.proposal_one_activity,
    acts.close_one_activity,
}


class _Recorder:
    """Fake ``workflow.execute_activity`` returning canned per-activity results."""

    def __init__(
        self,
        *,
        prospects,
        dossier_map,
        stopped=False,
        gate=True,
        qual_action="advance",
        raise_qualify_for=(),
        prepare_exc=None,
        dossier_maps=None,
    ):
        self.prospects = prospects
        self.dossier_map = dossier_map
        # Optional queue of dossier maps returned by successive load calls (to
        # exercise the proposal-stage re-load); falls back to dossier_map.
        self.dossier_maps = list(dossier_maps) if dossier_maps is not None else None
        self.stopped = stopped
        self.gate = gate
        self.qual_action = qual_action
        self.raise_qualify_for = set(raise_qualify_for)
        self.prepare_exc = prepare_exc
        self.calls = []  # (fn, args, retry_policy)
        self.finalize_result = None

    async def execute_activity(self, fn, *args, **kwargs):
        a = kwargs.get("args")
        self.calls.append((fn, a, kwargs.get("retry_policy")))
        if fn is acts.prepare_sales_pipeline_activity:
            if self.prepare_exc is not None:
                raise self.prepare_exc
            return {"stopped": self.stopped, "job_id": "job-wf"}
        if fn is acts.report_progress_activity:
            return self.gate
        if fn is acts.prospect_activity:
            return self.prospects
        if fn is acts.load_dossiers_activity:
            if self.dossier_maps:
                return self.dossier_maps.pop(0)
            return self.dossier_map
        if fn is acts.qualify_one_activity:
            prospect = a[1]
            if prospect["id"] in self.raise_qualify_for:
                raise RuntimeError(f"qualify boom for {prospect['id']}")
            return {"prospect": prospect, "recommended_action": self.qual_action}
        if fn in _PER_PROSPECT:
            return {"prospect": a[1]}
        if fn is acts.coach_activity:
            return {"overall_health": "ok"}
        if fn is acts.mark_failed_activity:
            return None
        if fn is acts.finalize_sales_pipeline_activity:
            self.finalize_result = a[1]
            return {"job_id": "job-wf"}
        raise AssertionError(f"unexpected activity: {fn}")

    def names(self):
        return [f.__name__ for (f, _a, _r) in self.calls]

    def count(self, fn):
        return sum(1 for (f, _a, _r) in self.calls if f is fn)

    def args_for(self, fn):
        return [a for (f, a, _r) in self.calls if f is fn]

    def retry_for(self, fn):
        return [r for (f, _a, r) in self.calls if f is fn]


def _run(monkeypatch, rec, request=None):
    monkeypatch.setattr(wf.workflow, "execute_activity", rec.execute_activity)
    return asyncio.run(wf.SalesWorkflow().run("job-wf", request or _REQUEST))


def _prospect(pid):
    return {"id": pid, "company_name": pid.upper()}


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------


def test_full_pipeline_order_and_fanout(monkeypatch):
    rec = _Recorder(prospects=[_prospect("p1"), _prospect("p2")], dossier_map={"p1": {"x": 1}})
    out = _run(monkeypatch, rec)

    assert out == {"job_id": "job-wf"}
    names = rec.names()
    assert names[0] == "prepare_sales_pipeline_activity"
    assert names[-1] == "finalize_sales_pipeline_activity"
    assert rec.count(acts.qualify_one_activity) == 2  # both prospects
    assert rec.count(acts.outreach_one_activity) == 1  # only p1 has a dossier
    assert rec.count(acts.discovery_one_activity) == 2  # both advanced
    assert rec.count(acts.proposal_one_activity) == 2
    assert rec.count(acts.close_one_activity) == 2
    assert rec.count(acts.coach_activity) == 1
    assert rec.count(acts.nurture_one_activity) == 0  # all-advance
    assert rec.count(acts.mark_failed_activity) == 0  # success path


def test_prospect_receives_existing_prospects_arg(monkeypatch):
    rec = _Recorder(prospects=[_prospect("p1")], dossier_map={})
    req = dict(_REQUEST, existing_prospects=[_prospect("seed")])
    _run(monkeypatch, rec, req)
    # prospect_activity is called with (ctx, existing_prospects)
    ((_ctx, existing),) = rec.args_for(acts.prospect_activity)
    assert existing == [_prospect("seed")]


def test_per_prospect_argument_pairing(monkeypatch):
    """Each fan-out activity must receive the CORRECT prospect-scoped extra
    argument — the seam the decomposition introduces. dossier_map/qual_by_id/
    prop_by_id must pair by the activity's own prospect id."""
    rec = _Recorder(
        prospects=[_prospect("p1"), _prospect("p2")],
        dossier_map={"p1": {"d": "p1"}, "p2": {"d": "p2"}},
    )
    _run(monkeypatch, rec)

    # outreach: [ctx, prospect, dossier] — dossier matches the prospect
    for _ctx, prospect, dossier in rec.args_for(acts.outreach_one_activity):
        assert dossier == {"d": prospect["id"]}
    # discovery: [ctx, prospect, qual, dossier] — both keyed to the prospect
    for _ctx, prospect, qual, dossier in rec.args_for(acts.discovery_one_activity):
        assert qual["prospect"]["id"] == prospect["id"]
        assert dossier == {"d": prospect["id"]}
    # proposal: [ctx, prospect, dossier, qual] — both keyed to the prospect
    for _ctx, prospect, dossier, qual in rec.args_for(acts.proposal_one_activity):
        assert dossier == {"d": prospect["id"]}
        assert qual["prospect"]["id"] == prospect["id"]
    # close: [ctx, prospect, proposal] — proposal's prospect matches
    for _ctx, prospect, proposal in rec.args_for(acts.close_one_activity):
        assert proposal["prospect"]["id"] == prospect["id"]


def test_retry_policies_per_activity(monkeypatch):
    rec = _Recorder(prospects=[_prospect("p1")], dossier_map={"p1": {"x": 1}})
    _run(monkeypatch, rec)

    for fn in (
        acts.prepare_sales_pipeline_activity,
        acts.report_progress_activity,
        acts.load_dossiers_activity,
        acts.finalize_sales_pipeline_activity,
    ):
        assert all(r is wf.IO_RETRY for r in rec.retry_for(fn)), fn.__name__
    for fn in (
        acts.prospect_activity,
        acts.qualify_one_activity,
        acts.outreach_one_activity,
        acts.proposal_one_activity,
        acts.coach_activity,
    ):
        assert all(r is wf.LLM_RETRY for r in rec.retry_for(fn)), fn.__name__


def test_progress_gates_write_entry_and_exit(monkeypatch):
    """Each run stage writes both its entry and exit progress pct (parity with
    the thread path's two-update-per-stage bar)."""
    rec = _Recorder(prospects=[_prospect("p1")], dossier_map={"p1": {"x": 1}})
    _run(monkeypatch, rec)
    pcts = [a[2] for a in rec.args_for(acts.report_progress_activity)]
    # prospecting entry+exit, outreach entry+exit, qualification entry+exit, then
    # discovery/proposal/negotiation entry+exit, plus coaching entry.
    assert 5 in pcts and 15 in pcts  # prospecting entry + exit
    assert 20 in pcts and 35 in pcts  # outreach entry + exit
    assert 40 in pcts and 50 in pcts  # qualification entry + exit


# ---------------------------------------------------------------------------
# Short-circuits + failure
# ---------------------------------------------------------------------------


def test_stopped_prepare_short_circuits(monkeypatch):
    rec = _Recorder(prospects=[], dossier_map={}, stopped=True)
    out = _run(monkeypatch, rec)
    assert out == {"job_id": "job-wf"}
    assert rec.names() == ["prepare_sales_pipeline_activity"]


def test_no_prospects_early_exit_goes_straight_to_finalize(monkeypatch):
    rec = _Recorder(prospects=[], dossier_map={})
    _run(monkeypatch, rec)
    # prospecting still writes entry+exit progress (parity with thread mode's
    # unconditional 5→15), then the empty result routes straight to finalize —
    # no dossier load, no stage fan-out.
    assert rec.names() == [
        "prepare_sales_pipeline_activity",
        "report_progress_activity",  # prospecting entry gate (5)
        "prospect_activity",
        "report_progress_activity",  # prospecting exit gate (15)
        "finalize_sales_pipeline_activity",
    ]
    assert rec.count(acts.load_dossiers_activity) == 0


def test_pipeline_error_marks_failed_and_reraises(monkeypatch):
    """A fatal error (here: prepare raising) is recorded via sales_mark_failed by
    the workflow's catch-all, then re-raised so the Temporal workflow fails."""
    rec = _Recorder(prospects=[], dossier_map={}, prepare_exc=RuntimeError("prepare boom"))

    with pytest.raises(RuntimeError, match="prepare boom"):
        _run(monkeypatch, rec)
    assert rec.count(acts.mark_failed_activity) == 1
    ((_job, err),) = rec.args_for(acts.mark_failed_activity)
    assert "prepare boom" in err


def test_native_cancellation_propagates_without_mark_failed(monkeypatch):
    """Temporal CancelledError must re-raise without scheduling sales_mark_failed.

    Unlike asyncio.CancelledError (a BaseException), temporalio's CancelledError
    subclasses Exception and would otherwise be caught by the catch-all and
    incorrectly mark a cancelled job FAILED.
    """
    rec = _Recorder(
        prospects=[],
        dossier_map={},
        prepare_exc=CancelledError("workflow cancelled"),
    )

    with pytest.raises(CancelledError):
        _run(monkeypatch, rec)

    assert rec.count(acts.mark_failed_activity) == 0


def test_activity_error_wrapping_cancellation_propagates_without_mark_failed(monkeypatch):
    """Cancelled activities surface as ActivityError with a CancelledError cause.

    A plain ``except CancelledError`` misses that shape; only
    ``is_cancelled_exception`` prevents the catch-all from marking FAILED.
    """
    err = ActivityError(
        "activity cancelled",
        scheduled_event_id=1,
        started_event_id=2,
        identity="test",
        activity_type="sales_prepare",
        activity_id="1",
        retry_state=None,
    )
    err.__cause__ = CancelledError("cancelled")
    rec = _Recorder(prospects=[], dossier_map={}, prepare_exc=err)

    with pytest.raises(ActivityError):
        _run(monkeypatch, rec)

    assert rec.count(acts.mark_failed_activity) == 0


def test_coaching_failure_is_absorbed(monkeypatch):
    """An infrastructure failure in the coach activity must not fail the run —
    the workflow continues to finalize without a coaching report."""
    monkeypatch.setattr(wf.workflow, "logger", MagicMock())  # no real workflow loop here
    rec = _Recorder(prospects=[_prospect("p1")], dossier_map={})

    orig = rec.execute_activity

    async def _wrapped(fn, *args, **kwargs):
        if fn is acts.coach_activity:
            raise RuntimeError("coach infra boom")
        return await orig(fn, *args, **kwargs)

    monkeypatch.setattr(wf.workflow, "execute_activity", _wrapped)
    out = asyncio.run(wf.SalesWorkflow().run("job-wf", _REQUEST))
    assert out == {"job_id": "job-wf"}
    assert rec.count(acts.finalize_sales_pipeline_activity) == 1
    assert rec.finalize_result["coaching_report"] is None


def test_mark_failed_failure_is_swallowed(monkeypatch):
    """If recording FAILED itself fails, the workflow still re-raises the
    original pipeline error (the FAILED-write failure is only logged)."""
    monkeypatch.setattr(wf.workflow, "logger", MagicMock())  # no real workflow loop here
    rec = _Recorder(prospects=[], dossier_map={}, prepare_exc=RuntimeError("prepare boom"))
    orig = rec.execute_activity

    async def _wrapped(fn, *args, **kwargs):
        if fn is acts.mark_failed_activity:
            raise RuntimeError("mark-failed store down")
        return await orig(fn, *args, **kwargs)

    monkeypatch.setattr(wf.workflow, "execute_activity", _wrapped)

    with pytest.raises(RuntimeError, match="prepare boom"):
        asyncio.run(wf.SalesWorkflow().run("job-wf", _REQUEST))


def test_cancel_gate_stops_further_stages(monkeypatch):
    rec = _Recorder(prospects=[_prospect("p1")], dossier_map={"p1": {}}, gate=False)
    _run(monkeypatch, rec)
    assert rec.count(acts.prospect_activity) == 0
    assert rec.count(acts.qualify_one_activity) == 0
    assert rec.count(acts.finalize_sales_pipeline_activity) == 1


# ---------------------------------------------------------------------------
# Entry-stage gating + routing + reload
# ---------------------------------------------------------------------------


def test_entry_discovery_skips_earlier_stages(monkeypatch):
    request = dict(
        _REQUEST, entry_stage="discovery", existing_prospects=[_prospect("p1"), _prospect("p2")]
    )
    rec = _Recorder(prospects=[], dossier_map={"p1": {}})
    _run(monkeypatch, rec, request)

    assert rec.count(acts.prospect_activity) == 0
    assert rec.count(acts.outreach_one_activity) == 0
    assert rec.count(acts.qualify_one_activity) == 0
    assert rec.count(acts.nurture_one_activity) == 0
    assert rec.count(acts.discovery_one_activity) == 2
    assert rec.count(acts.proposal_one_activity) == 2
    assert rec.count(acts.close_one_activity) == 2


def test_all_nurture_skips_advance_stages(monkeypatch):
    rec = _Recorder(
        prospects=[_prospect("p1"), _prospect("p2")], dossier_map={}, qual_action="nurture"
    )
    _run(monkeypatch, rec)
    assert rec.count(acts.nurture_one_activity) == 2
    assert rec.count(acts.discovery_one_activity) == 0
    assert rec.count(acts.proposal_one_activity) == 0
    assert rec.count(acts.close_one_activity) == 0


def test_discovery_stage_reloads_dossiers_when_first_load_empty(monkeypatch):
    """Thread-path parity: an empty first dossier load (e.g. transient outage)
    is retried at the discovery boundary (the first stage after outreach to
    consult the map) so discovery — and, since the map is now populated,
    proposal too — can still get grounding."""
    rec = _Recorder(
        prospects=[_prospect("p1")],
        dossier_map={},
        dossier_maps=[{}, {"p1": {"d": "p1"}}],  # first load empty, reload populated
    )
    _run(monkeypatch, rec)
    assert rec.count(acts.load_dossiers_activity) == 2  # initial + discovery reload
    # discovery received the reloaded dossier for its prospect
    ((_ctx, _p, _qual, dossier),) = rec.args_for(acts.discovery_one_activity)
    assert dossier == {"d": "p1"}
    # proposal reuses the same (already-populated) map — no second reload
    ((_ctx, prospect, dossier, _qual),) = rec.args_for(acts.proposal_one_activity)
    assert dossier == {"d": "p1"}


def test_failed_fanout_item_is_dropped(monkeypatch):
    rec = _Recorder(
        prospects=[_prospect("p1"), _prospect("p2")],
        dossier_map={},
        raise_qualify_for=["p2"],
    )
    _run(monkeypatch, rec)
    assert len(rec.finalize_result["qualified_leads"]) == 1
    assert rec.finalize_result["qualified_leads"][0]["prospect"]["id"] == "p1"
    assert rec.count(acts.proposal_one_activity) == 1
