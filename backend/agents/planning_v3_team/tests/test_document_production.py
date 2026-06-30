"""Tests for architecture-overview compaction in document production (no truncation)."""

import sys
from pathlib import Path

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from planning_v3_team.models import ClientContext  # noqa: E402
from planning_v3_team.phases.document_production import run_document_production  # noqa: E402


def _base_context(tmp_path):
    return {
        "repo_path": str(tmp_path),
        "client_context": ClientContext(client_name="Acme"),
        "spec_content": "# Spec\n\nbody",
    }


def test_architecture_overview_compacted_not_truncated(tmp_path, monkeypatch):
    """An oversized architecture overview is compacted (via compact_text), never sliced."""
    big = "A" * 9000  # > 8000 budget
    # document_production imports these at module top, so patch them in that module.
    monkeypatch.setattr(
        "planning_v3_team.phases.document_production.get_client",
        lambda agent_key=None: object(),  # not actually used; compact_text is patched
    )
    monkeypatch.setattr(
        "planning_v3_team.phases.document_production.compact_text",
        lambda text, *, max_chars, llm, content_description: "COMPACTED OVERVIEW",
    )

    ctx_update, _ = run_document_production(
        _base_context(tmp_path),
        use_product_analysis=False,
        use_planning_v2=False,
        run_architecture_fn=lambda **kw: big,
    )
    overview = ctx_update["handoff_package"].architecture_overview
    assert overview == "COMPACTED OVERVIEW"
    assert "(truncated)" not in overview  # never the old slice marker


def test_architecture_overview_bounded_on_compaction_failure(tmp_path, monkeypatch):
    """When compaction can't run, the overview is bounded to the budget (last resort)."""
    from planning_v3_team.phases.document_production import ARCHITECTURE_OVERVIEW_MAX_CHARS

    big = "B" * 9000

    def boom(*a, **k):
        raise RuntimeError("no client")

    monkeypatch.setattr("planning_v3_team.phases.document_production.get_client", boom)

    ctx_update, _ = run_document_production(
        _base_context(tmp_path),
        use_product_analysis=False,
        use_planning_v2=False,
        run_architecture_fn=lambda **kw: big,
    )
    overview = ctx_update["handoff_package"].architecture_overview
    assert overview == big[:ARCHITECTURE_OVERVIEW_MAX_CHARS]
    assert len(overview) == ARCHITECTURE_OVERVIEW_MAX_CHARS


def test_architecture_overview_hard_capped_when_compaction_overshoots(tmp_path, monkeypatch):
    """If compact_text returns more than the budget, a last-resort hard cap applies."""
    from planning_v3_team.phases.document_production import ARCHITECTURE_OVERVIEW_MAX_CHARS

    monkeypatch.setattr(
        "planning_v3_team.phases.document_production.get_client",
        lambda agent_key=None: object(),
    )
    # compact_text "fails to compress" and returns something still over budget.
    monkeypatch.setattr(
        "planning_v3_team.phases.document_production.compact_text",
        lambda text, *, max_chars, llm, content_description: "C" * 12000,
    )
    ctx_update, _ = run_document_production(
        _base_context(tmp_path),
        use_product_analysis=False,
        use_planning_v2=False,
        run_architecture_fn=lambda **kw: "A" * 9000,
    )
    assert (
        len(ctx_update["handoff_package"].architecture_overview) == ARCHITECTURE_OVERVIEW_MAX_CHARS
    )


def test_compact_architecture_overview_uses_injected_llm(monkeypatch):
    """When an llm is passed, the helper uses it directly and does NOT call get_client."""
    from planning_v3_team.phases import document_production as dp

    def fail_get_client(*a, **k):
        raise AssertionError("get_client should not be called when llm is provided")

    monkeypatch.setattr(dp, "get_client", fail_get_client)
    monkeypatch.setattr(
        dp,
        "compact_text",
        lambda text, *, max_chars, llm, content_description: f"COMPACTED:{llm}",
    )
    out = dp._compact_architecture_overview("X" * 9000, llm="INJECTED")
    assert out == "COMPACTED:INJECTED"


def test_architecture_overview_small_unchanged(tmp_path):
    """An overview within budget passes through unchanged (no compaction call)."""
    small = "short overview"
    ctx_update, _ = run_document_production(
        _base_context(tmp_path),
        use_product_analysis=False,
        use_planning_v2=False,
        run_architecture_fn=lambda **kw: small,
    )
    assert ctx_update["handoff_package"].architecture_overview == small
