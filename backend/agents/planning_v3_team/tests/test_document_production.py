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


def test_architecture_overview_kept_full_on_compaction_failure(tmp_path, monkeypatch):
    """When compaction can't run (client unavailable), the FULL overview is preserved."""
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
    # Full overview preserved (never sliced) when compaction can't run.
    assert ctx_update["handoff_package"].architecture_overview == big


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
