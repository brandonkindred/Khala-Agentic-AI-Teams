"""
Document production phase: produce context doc and spec; call PRA and optionally Planning V2.

Persists artifacts to repo path and builds handoff package.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from llm_service import compact_text, get_client
from planning_team.models import ClientContext, HandoffPackage

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

    Adapters are injected (run_pra, wait_pra, etc.) so tests can mock them.
    answer_callback(pending_questions) should return list of {question_id, selected_option_id, other_text?}
    for PRA when waiting_for_answers. If None, PRA may block on questions.
    Returns (context_update, artifacts). artifacts includes handoff_package.
    """
    repo_path = context.get("repo_path", "")
    client_context = context.get("client_context")
    if isinstance(client_context, dict):
        client_context = ClientContext(**client_context)
    spec_content = context.get("spec_content") or ""
    initial_brief = context.get("initial_brief") or ""
    spec_to_use = spec_content or initial_brief or "# Specification\n\n(To be refined.)"

    context_update: Dict[str, Any] = {}
    artifacts: Dict[str, Any] = {}
    client_context_doc_path: Optional[str] = None
    validated_spec_path: Optional[str] = None
    prd_path: Optional[str] = None

    path = Path(repo_path)
    path.mkdir(parents=True, exist_ok=True)
    (path / "plan").mkdir(parents=True, exist_ok=True)

    if client_context:
        client_context_doc_path = _write_context_document(repo_path, client_context)
        artifacts["client_context_document_path"] = client_context_doc_path

    initial_spec_path = _write_initial_spec(repo_path, spec_to_use)
    artifacts["initial_spec_path"] = initial_spec_path

    if use_product_analysis and run_pra and wait_pra:
        job_id = run_pra(repo_path=repo_path, spec_content=spec_to_use)
        if job_id:
            final = wait_pra(job_id=job_id, answer_callback=answer_callback)
            if final.get("status") == "completed":
                validated_spec_path = final.get("validated_spec_path")
                if not validated_spec_path:
                    validated_spec_path = str(
                        Path(repo_path) / "plan" / "product_analysis" / "validated_spec.md"
                    )
                prd_path = str(
                    Path(repo_path)
                    / "plan"
                    / "product_analysis"
                    / "product_requirements_document.md"
                )
            else:
                logger.warning("PRA did not complete: %s", final.get("error"))
        else:
            logger.warning("PRA run failed (no job_id)")
    else:
        validated_spec_path = initial_spec_path

    def _read_if_exists(p: Optional[str]) -> Optional[str]:
        if not p:
            return None
        path = Path(p)
        return path.read_text(encoding="utf-8") if path.exists() else None

    architecture_overview: Optional[str] = None
    if run_architecture_fn:
        try:
            spec_content_for_arch = _read_if_exists(validated_spec_path) or spec_to_use
            prd_content_for_arch = _read_if_exists(prd_path)
            cc_dict: Optional[Dict[str, Any]] = None
            if client_context and hasattr(client_context, "model_dump"):
                cc_dict = client_context.model_dump()
            elif isinstance(context.get("client_context"), dict):
                cc_dict = context["client_context"]
            architecture_overview = run_architecture_fn(
                spec_content=spec_content_for_arch or "",
                prd_content=prd_content_for_arch,
                repo_path=repo_path,
                client_context=cc_dict,
            )
            if (
                architecture_overview
                and len(architecture_overview) > ARCHITECTURE_OVERVIEW_MAX_CHARS
            ):
                architecture_overview = _compact_architecture_overview(architecture_overview)
        except Exception as e:
            logger.warning("Architecture step failed: %s", e)

    handoff = HandoffPackage(
        client_context=client_context,
        client_context_document_path=client_context_doc_path,
        validated_spec_path=validated_spec_path,
        validated_spec_content=_read_if_exists(validated_spec_path),
        prd_path=prd_path,
        prd_content=_read_if_exists(prd_path),
        architecture_overview=architecture_overview,
        summary="Handoff package produced by Planning.",
    )
    context_update["handoff_package"] = handoff
    artifacts["handoff_package"] = handoff.model_dump()
    return context_update, artifacts
