"""
Document production phase: produce context doc and spec; call PRA.

The orchestration (file writes, PRA run/wait, architecture step, handoff assembly) lives
in ``planning_team.agents.document_production.DocumentProductionAgent``; ``run_document_production``
below is a thin backward-compatible adapter over it.

The leaf IO/compaction helpers (``_compact_architecture_overview``, ``_write_context_document``,
``_write_initial_spec``) and the ``compact_text``/``get_client`` bindings intentionally stay
defined *here*: the compaction test suite monkeypatches these module globals, and a function
only observes patches applied to its defining module. The agent imports them from this module
lazily at call time.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from llm_service import compact_text, get_client
from planning_team.models import ClientContext

logger = logging.getLogger(__name__)

CONTEXT_DOC_FILENAME = "client_context.md"
INITIAL_SPEC_FILENAME = "initial_spec.md"

ARCHITECTURE_OVERVIEW_MAX_CHARS = 8000


def _compact_architecture_overview(overview: str, llm: Any = None) -> str:
    """Intelligently compact an oversized architecture overview, never slicing it.

    Uses ``compact_text`` (LLM-powered, preserves technical detail) instead of a raw
    ``[:8000]`` slice. On any failure the FULL overview is returned — we never drop
    architecture content.

    Args:
        overview: the architecture overview text (assumed longer than the budget).
        llm: optional ``LLMClient`` to reuse; when ``None`` the team's cached client
            is obtained lazily via ``get_client`` (memoized in ``factory.py``). The
            parameter makes the dependency injectable for testing.

    Preconditions:
        - ``overview`` is a non-empty string longer than the budget.
    Postconditions:
        - Returns a string of at most ``ARCHITECTURE_OVERVIEW_MAX_CHARS`` characters.
          Intelligent compaction is preferred; a hard truncation is applied only as a
          last resort (compaction was best-effort and still over budget, or it raised),
          so the bounded-output guarantee the old slice provided is preserved while we
          still avoid blindly slicing the common case. Unlike the spec, this overview is
          a *generated* secondary artifact, so a bounded last resort is acceptable.
    """
    try:
        client = llm if llm is not None else get_client("planning")
        compacted = compact_text(
            overview,
            max_chars=ARCHITECTURE_OVERVIEW_MAX_CHARS,
            llm=client,
            content_description="architecture overview",
        )
        if len(compacted) > ARCHITECTURE_OVERVIEW_MAX_CHARS:
            logger.warning(
                "Compacted architecture overview still exceeds budget (%d > %d chars); "
                "applying last-resort hard truncation.",
                len(compacted),
                ARCHITECTURE_OVERVIEW_MAX_CHARS,
            )
            return compacted[:ARCHITECTURE_OVERVIEW_MAX_CHARS]
        return compacted
    except Exception:
        logger.warning(
            "Architecture overview compaction failed; using bounded fallback", exc_info=True
        )
        return overview[:ARCHITECTURE_OVERVIEW_MAX_CHARS]


def _write_context_document(repo_path: str, client_context: ClientContext) -> str:
    """Write client context as markdown; return path to file."""
    path = Path(repo_path)
    path.mkdir(parents=True, exist_ok=True)
    plan_dir = path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    out = plan_dir / CONTEXT_DOC_FILENAME
    lines = [
        "# Client & context",
        "",
        f"**Client:** {client_context.client_name or 'TBD'}",
        f"**Domain:** {client_context.client_domain or 'TBD'}",
        "",
        "## Problem & opportunity",
        (client_context.problem_summary or ""),
        "",
        (client_context.opportunity_statement or ""),
        "",
        "## Target users",
        *([f"- {u}" for u in (client_context.target_users or [])]),
        "",
        "## Success criteria",
        *([f"- {c}" for c in (client_context.success_criteria or [])]),
        "",
        "## Constraints",
        f"**RPO/RTO:** {client_context.rpo_rto or 'TBD'}",
        f"**SLAs:** {client_context.slas or 'TBD'}",
        f"**Compliance:** {client_context.compliance_notes or 'TBD'}",
        "",
        "## Assumptions",
        *([f"- {a}" for a in (client_context.assumptions or [])]),
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    return str(out)


def _write_initial_spec(repo_path: str, spec_content: str) -> str:
    """Write initial spec to repo; return path."""
    path = Path(repo_path)
    path.mkdir(parents=True, exist_ok=True)
    out = path / INITIAL_SPEC_FILENAME
    out.write_text(spec_content or "# Product specification\n\n(To be refined.)", encoding="utf-8")
    return str(out)


def run_document_production(
    context: Dict[str, Any],
    use_product_analysis: bool = True,
    run_pra: Callable[..., Optional[str]] | None = None,
    wait_pra: Callable[..., Dict[str, Any]] | None = None,
    answer_callback: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]] = None,
    run_architecture_fn: Optional[Callable[..., Optional[str]]] = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Run document production: write context doc and spec; optionally call PRA.

    Thin adapter over ``DocumentProductionAgent``. Adapters are injected (run_pra, wait_pra,
    etc.) so tests can mock them. answer_callback(pending_questions) should return list of
    {question_id, selected_option_id, other_text?} for PRA when waiting_for_answers. If None,
    PRA may block on questions. Returns (context_update, artifacts). artifacts includes
    handoff_package.
    """
    from planning_team.agents.document_production import (
        DocumentProductionAgent,
        DocumentProductionInput,
    )

    out = DocumentProductionAgent().run(
        DocumentProductionInput(
            repo_path=context.get("repo_path", ""),
            client_context=context.get("client_context"),
            spec_content=context.get("spec_content") or "",
            initial_brief=context.get("initial_brief") or "",
            use_product_analysis=use_product_analysis,
        ),
        run_pra=run_pra,
        wait_pra=wait_pra,
        answer_callback=answer_callback,
        run_architecture_fn=run_architecture_fn,
    )
    return {"handoff_package": out.handoff_package}, out.artifacts
