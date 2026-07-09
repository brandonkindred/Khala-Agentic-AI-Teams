"""Tests for ``SalesWorkflow`` orchestration.

The workflow body is exercised directly with ``workflow.execute_activity``
monkeypatched to a recorder that returns canned activity results — the same
lightweight technique the previous single-activity test used. This asserts the
deterministic control flow: stage ordering, per-prospect fan-out, entry-stage
gating, advance/nurture routing, the no-prospects early-exit, the ``stopped``
short-circuit, per-item failure drop, and per-activity retry policies.
"""

from __future__ import annotations

import asyncio

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
    ):
        self.prospects = prospects
        self.dossier_map = dossier_map
        self.stopped = stopped
        self.gate = gate
        self.qual_action = qual_action
        self.raise_qualify_for = set(raise_qualify_for)
        self.calls = []  # (fn, args, retry_policy)
        self.finalize_result = None

    async def execute_activity(self, fn, *args, **kwargs):
        a = kwargs.get("args")
        self.calls.append((fn, a, kwargs.get("retry_policy")))
        if fn is acts.prepare_sales_pipeline_activity:
            return {"stopped": self.stopped, "job_id": "job-wf"}
        if fn is acts.report_progress_activity:
            return self.gate
        if fn is acts.prospect_activity:
            return self.prospects
        if fn is acts.load_dossiers_activity:
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
        if fn is acts.finalize_sales_pipeline_activity:
            self.finalize_result = a[1]
            return {"job_id": "job-wf"}
        raise AssertionError(f"unexpected activity: {fn}")

    def names(self):
        return [f.__name__ for (f, _a, _r) in self.calls]

    def count(self, fn):
        return sum(1 for (f, _a, _r) in self.calls if f is fn)

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
    # per-prospect fan-out counts
    assert rec.count(acts.qualify_one_activity) == 2  # both prospects
    assert rec.count(acts.outreach_one_activity) == 1  # only p1 has a dossier
    assert rec.count(acts.discovery_one_activity) == 2  # both advanced
    assert rec.count(acts.proposal_one_activity) == 2
    assert rec.count(acts.close_one_activity) == 2
    assert rec.count(acts.coach_activity) == 1
    # all-advance => no nurture stage
    assert rec.count(acts.nurture_one_activity) == 0


def test_retry_policies_per_activity(monkeypatch):
    rec = _Recorder(prospects=[_prospect("p1")], dossier_map={"p1": {"x": 1}})
    _run(monkeypatch, rec)

    # cheap idempotent job-store/DB activities => IO_RETRY
    for fn in (
        acts.prepare_sales_pipeline_activity,
        acts.report_progress_activity,
        acts.load_dossiers_activity,
        acts.finalize_sales_pipeline_activity,
    ):
        assert all(r is wf.IO_RETRY for r in rec.retry_for(fn)), fn.__name__
    # LLM activities => LLM_RETRY
    for fn in (
        acts.prospect_activity,
        acts.qualify_one_activity,
        acts.outreach_one_activity,
        acts.proposal_one_activity,
        acts.coach_activity,
    ):
        assert all(r is wf.LLM_RETRY for r in rec.retry_for(fn)), fn.__name__


# ---------------------------------------------------------------------------
# Short-circuits
# ---------------------------------------------------------------------------


def test_stopped_prepare_short_circuits(monkeypatch):
    rec = _Recorder(prospects=[], dossier_map={}, stopped=True)
    out = _run(monkeypatch, rec)
    assert out == {"job_id": "job-wf"}
    assert rec.names() == ["prepare_sales_pipeline_activity"]


def test_no_prospects_early_exit_goes_straight_to_finalize(monkeypatch):
    rec = _Recorder(prospects=[], dossier_map={})
    _run(monkeypatch, rec)
    assert rec.names() == [
        "prepare_sales_pipeline_activity",
        "report_progress_activity",  # prospecting gate
        "prospect_activity",
        "finalize_sales_pipeline_activity",
    ]


def test_cancel_gate_stops_further_stages(monkeypatch):
    """When the progress gate reports the job inactive, no stage fans out and
    finalize still runs (declining COMPLETED)."""
    rec = _Recorder(prospects=[_prospect("p1")], dossier_map={"p1": {}}, gate=False)
    _run(monkeypatch, rec)
    # prospecting gate returns False => prospect_activity never scheduled; the
    # workflow falls through to finalize with no prospects.
    assert rec.count(acts.prospect_activity) == 0
    assert rec.count(acts.qualify_one_activity) == 0
    assert rec.count(acts.finalize_sales_pipeline_activity) == 1


# ---------------------------------------------------------------------------
# Entry-stage gating + routing
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
    # discovery onward runs against the supplied existing prospects
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


# ---------------------------------------------------------------------------
# Per-item failure is dropped, not fatal
# ---------------------------------------------------------------------------


def test_failed_fanout_item_is_dropped(monkeypatch):
    rec = _Recorder(
        prospects=[_prospect("p1"), _prospect("p2")],
        dossier_map={},
        raise_qualify_for=["p2"],
    )
    _run(monkeypatch, rec)
    # p2's qualification raised => dropped; only p1 survives into qualified_leads
    assert len(rec.finalize_result["qualified_leads"]) == 1
    assert rec.finalize_result["qualified_leads"][0]["prospect"]["id"] == "p1"
    # only the surviving prospect advances downstream
    assert rec.count(acts.proposal_one_activity) == 1
