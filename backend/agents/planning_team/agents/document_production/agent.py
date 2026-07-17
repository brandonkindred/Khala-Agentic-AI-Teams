"""Document production agent: write context doc + spec, run PRA, assemble handoff.

§2 coordinator for the document-production phase: it plans the file writes, delegates
to the Product Analysis (``run_pra``/``wait_pra``) and Architecture (``run_architecture_fn``)
tools (§3), and assembles the ``HandoffPackage``.

Note: the leaf IO/compaction helpers (``_compact_architecture_overview``,
``_write_context_document``, ``_write_initial_spec``) and the ``get_client``/``compact_text``
bindings intentionally remain defined in ``planning_team.phases.document_production`` — the
compaction test suite monkeypatches those module globals, and a function only sees patches
applied to its *defining* module. They are imported here lazily (inside :meth:`run`) so the
phase adapter can import this agent at module top without a circular import.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from planning_team.agents.document_production.models import (
    DocumentProductionInput,
    DocumentProductionOutput,
)
from planning_team.models import HandoffPackage
from planning_team.phases._util import as_client_context

logger = logging.getLogger(__name__)


class DocumentProductionAgent:
    """Stateless agent that produces planning documents and the downstream handoff.

    Invariants:
        - Holds no mutable state; a single instance is safe to reuse across runs.
    """

    def run(
        self,
        input_data: DocumentProductionInput,
        *,
        run_pra: Callable[..., Optional[str]] | None = None,
        wait_pra: Callable[..., Dict[str, Any]] | None = None,
        answer_callback: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]] = None,
        run_architecture_fn: Optional[Callable[..., Optional[str]]] = None,
    ) -> DocumentProductionOutput:
        """Write context doc and spec, optionally call PRA, and build the handoff.

        Preconditions:
            - Adapters are injected: ``run_pra``/``wait_pra`` when product analysis is
              enabled; ``answer_callback(pending_questions)`` resolves PRA questions.
        Postconditions:
            - Returns a ``DocumentProductionOutput`` whose ``handoff_package`` carries the
              client context, validated spec / PRD paths and content, and (bounded)
              architecture overview, and whose ``artifacts`` indexes the written files.
        """
        # Imported lazily so the phase adapter can import this agent at module top; these
        # helpers stay in the phase module because the compaction tests monkeypatch its
        # ``get_client``/``compact_text`` globals (see module docstring).
        from planning_team.phases.document_production import (
            ARCHITECTURE_OVERVIEW_MAX_CHARS,
            _compact_architecture_overview,
            _write_context_document,
            _write_initial_spec,
        )

        repo_path = input_data.repo_path or ""
        raw_client_context = input_data.client_context
        client_context = as_client_context(raw_client_context)
        spec_content = input_data.spec_content or ""
        initial_brief = input_data.initial_brief or ""
        spec_to_use = spec_content or initial_brief or "# Specification\n\n(To be refined.)"
        use_product_analysis = input_data.use_product_analysis

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
                elif isinstance(raw_client_context, dict):  # pragma: no cover
                    # Defensive (carried over verbatim): unreachable in practice because
                    # ``as_client_context`` always turns a dict into a truthy ``ClientContext``,
                    # so this branch only executes if a raw dict survives normalization.
                    cc_dict = raw_client_context
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
        artifacts["handoff_package"] = handoff.model_dump()
        return DocumentProductionOutput(handoff_package=handoff, artifacts=artifacts)
