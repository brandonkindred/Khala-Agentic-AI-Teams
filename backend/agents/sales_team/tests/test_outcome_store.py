"""Tests for ``sales_team.outcome_store``.

The store persists ``StageOutcome`` and ``DealOutcome`` records as JSON
files plus a single ``LearningInsights`` snapshot. The module reads the
cache root at import time from ``AGENT_CACHE``, so each test reaches
into the module-level path constants and points them at a tmpdir.

These tests cover:

  * write/read round-trip for both outcome types,
  * id + timestamp assignment when caller leaves them empty,
  * outcome_counts before/after writes,
  * insights save/load round-trip,
  * defensive branches: corrupt JSON, missing directory, missing file.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest

from sales_team import outcome_store
from sales_team.models import (
    DealOutcome,
    DealResult,
    LearningInsights,
    OutcomeResult,
    PipelineStage,
    StageOutcome,
)


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the module-level paths at an empty per-test tmpdir."""
    cache_root = tmp_path / "sales_team" / "outcomes"
    insights = tmp_path / "sales_team" / "insights" / "current.json"
    monkeypatch.setattr(outcome_store, "_CACHE_ROOT", cache_root)
    monkeypatch.setattr(outcome_store, "_INSIGHTS_PATH", insights)
    return cache_root, insights


# ---------------------------------------------------------------------------
# Stage outcomes
# ---------------------------------------------------------------------------


def test_record_stage_outcome_assigns_id_and_timestamp_and_persists(
    _isolate_store,
) -> None:
    cache_root, _ = _isolate_store
    stage = StageOutcome(
        company_name="Acme Corp",
        stage=PipelineStage.OUTREACH,
        outcome=OutcomeResult.CONVERTED,
    )
    saved = outcome_store.record_stage_outcome(stage)

    assert saved.outcome_id, "outcome_id should be assigned by the store"
    assert saved.recorded_at, "recorded_at should be set by the store"
    expected_file = cache_root / "stage" / f"{saved.outcome_id}.json"
    assert expected_file.exists()


def test_record_stage_outcome_keeps_caller_provided_id_and_timestamp() -> None:
    stage = StageOutcome(
        outcome_id="stage-explicit-1",
        recorded_at="2026-05-21T00:00:00+00:00",
        company_name="Acme Corp",
        stage=PipelineStage.OUTREACH,
        outcome=OutcomeResult.CONVERTED,
    )
    saved = outcome_store.record_stage_outcome(stage)
    assert saved.outcome_id == "stage-explicit-1"
    assert saved.recorded_at == "2026-05-21T00:00:00+00:00"


def test_load_stage_outcomes_returns_empty_when_dir_missing(_isolate_store) -> None:
    # Nothing has been written yet — the stage subdir doesn't exist.
    assert outcome_store.load_stage_outcomes() == []


def test_load_stage_outcomes_returns_recorded_entries() -> None:
    a = outcome_store.record_stage_outcome(
        StageOutcome(
            company_name="Acme",
            stage=PipelineStage.OUTREACH,
            outcome=OutcomeResult.CONVERTED,
        )
    )
    b = outcome_store.record_stage_outcome(
        StageOutcome(
            company_name="Beta",
            stage=PipelineStage.QUALIFICATION,
            outcome=OutcomeResult.STALLED,
        )
    )
    loaded = outcome_store.load_stage_outcomes()
    ids = {o.outcome_id for o in loaded}
    assert {a.outcome_id, b.outcome_id} <= ids


def test_load_stage_outcomes_respects_limit() -> None:
    for i in range(5):
        outcome_store.record_stage_outcome(
            StageOutcome(
                company_name=f"Acme {i}",
                stage=PipelineStage.OUTREACH,
                outcome=OutcomeResult.CONVERTED,
            )
        )
    loaded = outcome_store.load_stage_outcomes(limit=2)
    assert len(loaded) == 2


def test_load_stage_outcomes_skips_corrupt_files(_isolate_store) -> None:
    cache_root, _ = _isolate_store
    # Persist a real outcome to set up the directory.
    saved = outcome_store.record_stage_outcome(
        StageOutcome(
            company_name="Acme",
            stage=PipelineStage.OUTREACH,
            outcome=OutcomeResult.CONVERTED,
        )
    )
    # Drop a malformed JSON file alongside it.
    (cache_root / "stage" / "bad.json").write_text("{ NOT JSON", encoding="utf-8")
    # And a JSON file that doesn't match the StageOutcome schema.
    (cache_root / "stage" / "schema-bad.json").write_text('{"foo": "bar"}', encoding="utf-8")

    loaded = outcome_store.load_stage_outcomes()
    # Real entry survives; corrupt ones are dropped with a warning, not raised.
    assert any(o.outcome_id == saved.outcome_id for o in loaded)


# ---------------------------------------------------------------------------
# Deal outcomes
# ---------------------------------------------------------------------------


def test_record_deal_outcome_round_trip(_isolate_store) -> None:
    cache_root, _ = _isolate_store
    deal = DealOutcome(
        company_name="Acme",
        final_stage_reached=PipelineStage.CLOSED_WON,
        result=DealResult.WON,
    )
    saved = outcome_store.record_deal_outcome(deal)
    assert saved.outcome_id
    assert (cache_root / "deal" / f"{saved.outcome_id}.json").exists()

    loaded = outcome_store.load_deal_outcomes()
    assert any(o.outcome_id == saved.outcome_id for o in loaded)


def test_load_deal_outcomes_returns_empty_when_dir_missing(_isolate_store) -> None:
    assert outcome_store.load_deal_outcomes() == []


def test_load_deal_outcomes_skips_corrupt_files(_isolate_store) -> None:
    cache_root, _ = _isolate_store
    saved = outcome_store.record_deal_outcome(
        DealOutcome(
            company_name="Acme",
            final_stage_reached=PipelineStage.CLOSED_WON,
            result=DealResult.WON,
        )
    )
    (cache_root / "deal" / "bad.json").write_text("not-json", encoding="utf-8")
    (cache_root / "deal" / "schema-bad.json").write_text('{"foo": 1}', encoding="utf-8")
    loaded = outcome_store.load_deal_outcomes()
    assert any(o.outcome_id == saved.outcome_id for o in loaded)


def test_load_deal_outcomes_respects_limit() -> None:
    for i in range(3):
        outcome_store.record_deal_outcome(
            DealOutcome(
                company_name=f"C{i}",
                final_stage_reached=PipelineStage.CLOSED_WON,
                result=DealResult.WON,
            )
        )
    assert len(outcome_store.load_deal_outcomes(limit=1)) == 1


# ---------------------------------------------------------------------------
# Insights round-trip
# ---------------------------------------------------------------------------


def test_save_and_load_insights_round_trip(_isolate_store) -> None:
    _, insights_path = _isolate_store
    assert outcome_store.load_current_insights() is None  # not yet generated

    insights = LearningInsights(
        total_outcomes_analyzed=3,
        win_rate=0.5,
        winning_patterns=["multi-thread"],
        insights_version=1,
        generated_at="2026-05-21T00:00:00+00:00",
    )
    outcome_store.save_insights(insights)
    assert insights_path.exists()

    loaded = outcome_store.load_current_insights()
    assert loaded is not None
    assert loaded.total_outcomes_analyzed == 3
    assert loaded.win_rate == 0.5
    assert loaded.winning_patterns == ["multi-thread"]


def test_load_current_insights_returns_none_on_unparseable_file(_isolate_store) -> None:
    _, insights_path = _isolate_store
    insights_path.parent.mkdir(parents=True, exist_ok=True)
    insights_path.write_text("garbage{", encoding="utf-8")
    assert outcome_store.load_current_insights() is None


def test_load_current_insights_returns_none_on_schema_mismatch(_isolate_store) -> None:
    _, insights_path = _isolate_store
    insights_path.parent.mkdir(parents=True, exist_ok=True)
    # Valid JSON but wrong shape — win_rate range violation.
    insights_path.write_text('{"win_rate": 2.0}', encoding="utf-8")
    assert outcome_store.load_current_insights() is None


# ---------------------------------------------------------------------------
# outcome_counts
# ---------------------------------------------------------------------------


def test_outcome_counts_when_nothing_written(_isolate_store) -> None:
    counts = outcome_store.outcome_counts()
    assert counts == {"stage_outcomes": 0, "deal_outcomes": 0, "has_insights": False}


def test_outcome_counts_after_writes(_isolate_store) -> None:
    outcome_store.record_stage_outcome(
        StageOutcome(
            company_name="A",
            stage=PipelineStage.OUTREACH,
            outcome=OutcomeResult.CONVERTED,
        )
    )
    outcome_store.record_deal_outcome(
        DealOutcome(
            company_name="A",
            final_stage_reached=PipelineStage.CLOSED_WON,
            result=DealResult.WON,
        )
    )
    outcome_store.save_insights(LearningInsights(insights_version=1))
    counts = outcome_store.outcome_counts()
    assert counts == {"stage_outcomes": 1, "deal_outcomes": 1, "has_insights": True}


# ---------------------------------------------------------------------------
# AGENT_CACHE env var resolution
# ---------------------------------------------------------------------------


def test_cache_paths_resolve_under_agent_cache_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The module must read its cache root from AGENT_CACHE (not AGENT_CACHE_DIR)."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    try:
        importlib.reload(outcome_store)
        assert outcome_store._CACHE_ROOT == tmp_path / "sales_team" / "outcomes"
        assert outcome_store._INSIGHTS_PATH == (
            tmp_path / "sales_team" / "insights" / "current.json"
        )
    finally:
        monkeypatch.delenv("AGENT_CACHE", raising=False)
        importlib.reload(outcome_store)


_DEAD_ENV_VAR_NAME = "AGENT_CACHE" + "_DIR"


def test_agent_cache_dir_env_var_never_read_in_sales_team() -> None:
    """Regression guard: the dead AGENT_CACHE_DIR name must not reappear in sales_team."""
    import sales_team

    package_dir = Path(inspect.getfile(sales_team)).parent
    this_file = Path(__file__).resolve()
    offenders = [
        py_file
        for py_file in package_dir.rglob("*.py")
        if py_file.resolve() != this_file
        and _DEAD_ENV_VAR_NAME in py_file.read_text(encoding="utf-8")
    ]
    assert not offenders, f"stale {_DEAD_ENV_VAR_NAME} reference(s) found in: {offenders}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def test_now_returns_iso_with_offset() -> None:
    now = outcome_store._now()
    assert "T" in now
    assert now.endswith("+00:00") or now.endswith("Z")


def test_read_json_returns_none_for_missing_path(tmp_path: Path) -> None:
    assert outcome_store._read_json(tmp_path / "does-not-exist.json") is None
