"""Tests for ``DeepResearchWorkflow`` orchestration.

The workflow body is exercised directly with ``workflow.execute_activity``
monkeypatched to a recorder returning canned activity results — asserting the
deterministic control flow: prepare → companies → per-company map fan-out →
rank → per-prospect dossier fan-out → finalize, the early exits, per-item drop,
the cancel-gate short-circuit, and the catch-all FAILED write.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from sales_team.temporal import activities as acts
from sales_team.temporal import deep_research_activities as dra
from sales_team.temporal import deep_research_workflow as wf

_REQUEST = {
    "product_name": "ProductX",
    "value_proposition": "Save 20% on outbound time",
    "icp": {"industry": ["SaaS"]},
    "target_prospects": 10,
    "max_per_company": 2,
}


def _company(cid):
    return {"id": cid, "company_name": cid.upper()}


def _prospect(pid):
    return {"id": pid, "company_name": pid.upper()}


class _Recorder:
    def __init__(
        self,
        *,
        companies,
        prospects,
        stopped=False,
        gate_false_stages=(),
        prepare_exc=None,
        raise_map_for=(),
        raise_dossier_for=(),
    ):
        self.companies = companies
        self.prospects = prospects
        self.stopped = stopped
        self.gate_false_stages = set(gate_false_stages)
        self.prepare_exc = prepare_exc
        self.raise_map_for = set(raise_map_for)
        self.raise_dossier_for = set(raise_dossier_for)
        self.calls = []
        self.finalize_args = None

    async def execute_activity(self, fn, *args, **kwargs):
        a = kwargs.get("args")
        self.calls.append((fn, a))
        if fn is dra.prepare_deep_research_activity:
            if self.prepare_exc is not None:
                raise self.prepare_exc
            return {"stopped": self.stopped, "job_id": "dr-wf"}
        if fn is acts.report_progress_activity:
            return a[1] not in self.gate_false_stages  # a = [job_id, stage, pct]
        if fn is dra.companies_activity:
            return self.companies
        if fn is dra.map_company_one_activity:
            company = a[1]
            if company["id"] in self.raise_map_for:
                raise RuntimeError(f"map boom {company['id']}")
            # one decision-maker prospect per company
            return [{"prospect": _prospect(f"p_{company['id']}"), "confidence": 0.8}]
        if fn is dra.rank_activity:
            return self.prospects
        if fn is dra.build_dossier_one_activity:
            prospect = a[1]
            if prospect["id"] in self.raise_dossier_for:
                raise RuntimeError(f"dossier boom {prospect['id']}")
            return {"prospect_id": prospect["id"], "dossier_id": f"dsr_{prospect['id']}"}
        if fn is acts.mark_failed_activity:
            return None
        if fn is dra.finalize_deep_research_activity:
            self.finalize_args = a
            return {"job_id": "dr-wf"}
        raise AssertionError(f"unexpected activity: {fn}")

    def names(self):
        return [f.__name__ for (f, _a) in self.calls]

    def count(self, fn):
        return sum(1 for (f, _a) in self.calls if f is fn)


def _run(monkeypatch, rec):
    monkeypatch.setattr(wf.workflow, "execute_activity", rec.execute_activity)
    return asyncio.run(wf.DeepResearchWorkflow().run("dr-wf", _REQUEST))


def test_full_pipeline_fans_out_and_finalizes(monkeypatch):
    rec = _Recorder(
        companies=[_company("c1"), _company("c2")],
        prospects=[_prospect("p1"), _prospect("p2")],
    )
    out = _run(monkeypatch, rec)
    assert out == {"job_id": "dr-wf"}
    assert rec.count(dra.map_company_one_activity) == 2  # one per company
    assert rec.count(dra.build_dossier_one_activity) == 2  # one per ranked prospect
    assert rec.count(dra.finalize_deep_research_activity) == 1
    # finalize received the ranked prospects + built dossiers
    _ctx, final_prospects, dossiers, notes = rec.finalize_args
    assert len(final_prospects) == 2 and len(dossiers) == 2 and notes == []


def test_stopped_prepare_short_circuits(monkeypatch):
    rec = _Recorder(companies=[], prospects=[], stopped=True)
    out = _run(monkeypatch, rec)
    assert out == {"job_id": "dr-wf"}
    assert rec.names() == ["prepare_deep_research_activity"]


def test_no_companies_finalizes_with_note(monkeypatch):
    rec = _Recorder(companies=[], prospects=[])
    _run(monkeypatch, rec)
    assert rec.count(dra.map_company_one_activity) == 0
    _ctx, fp, dossiers, notes = rec.finalize_args
    assert fp == [] and "No companies" in notes[0]


def test_no_decision_makers_finalizes_with_note(monkeypatch):
    # companies exist but every map call raises → mapped is empty
    rec = _Recorder(companies=[_company("c1")], prospects=[], raise_map_for=["c1"])
    _run(monkeypatch, rec)
    assert rec.count(dra.rank_activity) == 0
    _ctx, fp, dossiers, notes = rec.finalize_args
    assert "No decision-makers" in notes[0]


def test_empty_ranking_finalizes_without_dossiers(monkeypatch):
    """If ranking yields no prospects (e.g. all deduped/capped away), the run
    finalizes with an empty result and no dossier fan-out."""
    rec = _Recorder(companies=[_company("c1")], prospects=[])  # rank returns []
    _run(monkeypatch, rec)
    assert rec.count(dra.build_dossier_one_activity) == 0
    _ctx, fp, dossiers, _notes = rec.finalize_args
    assert fp == [] and dossiers == []


def test_failed_dossier_item_is_dropped(monkeypatch):
    rec = _Recorder(
        companies=[_company("c1")],
        prospects=[_prospect("p1"), _prospect("p2")],
        raise_dossier_for=["p2"],
    )
    _run(monkeypatch, rec)
    _ctx, fp, dossiers, notes = rec.finalize_args
    assert len(fp) == 2  # both ranked prospects still passed to finalize
    assert [d["prospect_id"] for d in dossiers] == ["p1"]  # p2's dossier dropped


def test_companies_gate_short_circuits_to_finalize(monkeypatch):
    rec = _Recorder(
        companies=[_company("c1")], prospects=[_prospect("p1")], gate_false_stages=["companies"]
    )
    _run(monkeypatch, rec)
    # first gate (companies) returns False → jump straight to finalize
    assert rec.count(dra.companies_activity) == 0
    assert rec.count(dra.finalize_deep_research_activity) == 1


@pytest.mark.parametrize(
    "false_stage,should_reach",
    [
        ("decision_makers", dra.companies_activity),
        ("ranking", dra.map_company_one_activity),
        ("dossiers", dra.rank_activity),
    ],
)
def test_later_stage_cancel_gate_short_circuits(monkeypatch, false_stage, should_reach):
    """A cancel detected at a later stage's entry gate stops fan-out and goes
    straight to finalize (which declines COMPLETED on a terminal job)."""
    rec = _Recorder(
        companies=[_company("c1")],
        prospects=[_prospect("p1")],
        gate_false_stages=[false_stage],
    )
    _run(monkeypatch, rec)
    assert rec.count(should_reach) >= 1  # reached the stage before the cancelled gate
    assert rec.count(dra.finalize_deep_research_activity) == 1
    # nothing after the cancelled gate ran
    if false_stage == "decision_makers":
        assert rec.count(dra.map_company_one_activity) == 0
    elif false_stage == "ranking":
        assert rec.count(dra.rank_activity) == 0
    else:
        assert rec.count(dra.build_dossier_one_activity) == 0


def test_pipeline_error_marks_failed_and_reraises(monkeypatch):
    rec = _Recorder(companies=[], prospects=[], prepare_exc=RuntimeError("prepare boom"))

    monkeypatch.setattr(wf.workflow, "logger", MagicMock())
    with pytest.raises(RuntimeError, match="prepare boom"):
        _run(monkeypatch, rec)
    assert rec.count(acts.mark_failed_activity) == 1


def test_mark_failed_failure_is_swallowed(monkeypatch):
    rec = _Recorder(companies=[], prospects=[], prepare_exc=RuntimeError("prepare boom"))
    orig = rec.execute_activity

    async def _wrapped(fn, *args, **kwargs):
        if fn is acts.mark_failed_activity:
            raise RuntimeError("mark-failed down")
        return await orig(fn, *args, **kwargs)

    monkeypatch.setattr(wf.workflow, "logger", MagicMock())
    monkeypatch.setattr(wf.workflow, "execute_activity", _wrapped)
    with pytest.raises(RuntimeError, match="prepare boom"):
        asyncio.run(wf.DeepResearchWorkflow().run("dr-wf", _REQUEST))
