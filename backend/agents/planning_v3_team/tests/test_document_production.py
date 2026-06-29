"""Tests for architecture-overview compaction (no truncation) and _get_llm wiring."""

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
    big = "A" * 9000  # > 8000 budget
    monkeypatch.setattr(
        "llm_service.get_client",
        lambda agent_key=None: object(),  # not actually used; compact_text is patched
    )
    monkeypatch.setattr(
        "llm_service.compact_text",
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
    big = "B" * 9000

    def boom(*a, **k):
        raise RuntimeError("no client")

    monkeypatch.setattr("llm_service.get_client", boom)

    ctx_update, _ = run_document_production(
        _base_context(tmp_path),
        use_product_analysis=False,
        use_planning_v2=False,
        run_architecture_fn=lambda **kw: big,
    )
    # Full overview preserved (never sliced) when compaction can't run.
    assert ctx_update["handoff_package"].architecture_overview == big


def test_architecture_overview_small_unchanged(tmp_path):
    small = "short overview"
    ctx_update, _ = run_document_production(
        _base_context(tmp_path),
        use_product_analysis=False,
        use_planning_v2=False,
        run_architecture_fn=lambda **kw: small,
    )
    assert ctx_update["handoff_package"].architecture_overview == small


def test_get_llm_returns_llm_client(monkeypatch):
    """_get_llm must return whatever get_client yields (a real LLMClient), not a Strands Agent."""
    from planning_v3_team.api import main as api_main

    sentinel = object()
    monkeypatch.setattr("llm_service.get_client", lambda agent_key=None: sentinel)
    assert api_main._get_llm() is sentinel
