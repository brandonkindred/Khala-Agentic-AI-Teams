"""
Artifact persistence for the blogging agent pipeline.

Provides helpers to write and read versioned artifacts to a work directory,
so the pipeline is auditable and repeatable.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)

# Canonical artifact filenames (per spec)
ARTIFACT_NAMES = (
    "brand_spec_prompt.md",
    "content_brief.md",
    "content_plan.json",
    "content_plan.md",
    "research_packet.md",
    "allowed_claims.json",
    "outline.md",
    "draft_v1.md",
    "draft_v2.md",
    "final.md",
    "compliance_report.json",
    "fact_check_report.json",
    "validator_report.json",
    "publishing_pack.json",
    "editor_feedback.json",
    "medium_stats_report.json",
)

# Static metadata: which pipeline phase/agent produces each artifact (for API list response)
ARTIFACT_PRODUCER: dict[str, dict[str, str]] = {
    "brand_spec_prompt.md": {
        "producer_phase": "draft_initial",
        "producer_agent": "Pipeline (brand load)",
    },
    "content_brief.md": {"producer_phase": "planning", "producer_agent": "BlogPlanningAgent"},
    "content_plan.json": {"producer_phase": "planning", "producer_agent": "BlogPlanningAgent"},
    "content_plan.md": {"producer_phase": "planning", "producer_agent": "BlogPlanningAgent"},
    "research_packet.md": {"producer_phase": "research", "producer_agent": "BlogResearchAgent"},
    "allowed_claims.json": {
        "producer_phase": "external",
        "producer_agent": "External input (BlogResearchAgent can build one via "
        "extract_allowed_claims(), but is currently standalone and not invoked "
        "by run_pipeline)",
    },
    "outline.md": {"producer_phase": "planning", "producer_agent": "BlogPlanningAgent"},
    "draft_v1.md": {"producer_phase": "draft_initial", "producer_agent": "BlogWriterAgent"},
    "draft_v2.md": {"producer_phase": "copy_edit", "producer_agent": "BlogCopyEditorAgent"},
    "final.md": {"producer_phase": "finalize", "producer_agent": "BlogCopyEditorAgent"},
    "compliance_report.json": {
        "producer_phase": "compliance",
        "producer_agent": "BlogComplianceAgent",
    },
    "fact_check_report.json": {
        "producer_phase": "fact_check",
        "producer_agent": "BlogFactCheckAgent",
    },
    "validator_report.json": {"producer_phase": "compliance", "producer_agent": "Validators"},
    "publishing_pack.json": {"producer_phase": "finalize", "producer_agent": "Pipeline"},
    "editor_feedback.json": {
        "producer_phase": "copy_edit",
        "producer_agent": "BlogCopyEditorAgent",
    },
    "medium_stats_report.json": {
        "producer_phase": "medium_stats",
        "producer_agent": "BlogMediumStatsAgent",
    },
}


def _resolve_work_dir(work_dir: Union[str, Path]) -> Path:
    """Resolve work_dir to an absolute Path; create if needed."""
    path = Path(work_dir).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_artifact(
    work_dir: Union[str, Path],
    name: str,
    content: Union[str, dict, list],
    *,
    return_path: bool = False,
) -> Optional[Path]:
    """
    Write an artifact to the work directory.

    Args:
        work_dir: Directory for run artifacts.
        name: Artifact filename (e.g. "research_packet.md", "allowed_claims.json").
        content: String content (for .md, .yaml) or dict/list (for .json; will be JSON-serialized).
        return_path: If True, return the written path; otherwise return None.

    Returns:
        Path to the written file if return_path is True, else None.
    """
    work_path = _resolve_work_dir(work_dir)
    out_file = work_path / name

    if isinstance(content, (dict, list)):
        if not name.endswith(".json"):
            raise ValueError(f"Dict/list content requires .json artifact name, got {name}")
        out_file.write_text(json.dumps(content, indent=2), encoding="utf-8")
    else:
        out_file.write_text(str(content), encoding="utf-8")

    logger.debug("Wrote artifact to %s", out_file)
    return out_file if return_path else None


def read_artifact(
    work_dir: Union[str, Path],
    name: str,
    *,
    default: Optional[Any] = None,
    parse_json: Optional[bool] = None,
) -> Optional[Union[str, dict, list]]:
    """
    Read an artifact from the work directory.

    Args:
        work_dir: Directory containing run artifacts.
        name: Artifact filename.
        default: Value to return if file does not exist.
        parse_json: If True, parse as JSON; if False, return raw string.
                    If None, infer from .json extension.

    Returns:
        File content as string or parsed JSON (dict/list), or default if not found.
    """
    work_path = Path(work_dir).resolve()
    out_file = work_path / name

    if not out_file.exists():
        return default

    raw = out_file.read_text(encoding="utf-8")

    if parse_json is None:
        parse_json = name.endswith(".json")
    if parse_json:
        return json.loads(raw)
    return raw


def load_allowed_claims_for_brief(
    work_dir: Optional[Union[str, Path]],
    brief_text: str,
) -> Optional[dict]:
    """
    Load allowed_claims.json from work_dir, but only if it belongs to the current brief.

    A work_dir may be reused across runs (e.g. a stable CLI run_dir, or a caller
    explicitly reusing work_dir for a new topic). Without this check, a stale
    allowed_claims.json left by an earlier, unrelated brief would silently
    constrain and validate the new draft's claims.

    Preconditions:
        brief_text is the current run's brief/topic string (may be "").
    Postconditions:
        Returns None when work_dir is falsy, the artifact is missing or not a
        dict, or its "topic" field doesn't equal brief_text exactly (a stale
        artifact from a reused work_dir). Otherwise returns ``{"topic": ...,
        "claims": [...]}`` with "claims" normalized to a list containing only
        the entries that are dicts with a truthy "id" and "text" — a
        non-list, missing, or malformed-entry "claims" value is never passed
        through as-is, so every consumer (writer prompt, fact-check agent)
        can rely on getting either a well-formed claim list or an empty one,
        never something that raises when a consumer calls ``.get()`` on an
        entry.
    """
    if not work_dir:
        return None
    allowed_claims = read_artifact(work_dir, "allowed_claims.json", default=None)
    if not isinstance(allowed_claims, dict):
        return None
    if allowed_claims.get("topic") != brief_text:
        return None
    claims = allowed_claims.get("claims")
    if not isinstance(claims, list):
        claims = []
    sanitized_claims = [c for c in claims if isinstance(c, dict) and c.get("id") and c.get("text")]
    return {"topic": brief_text, "claims": sanitized_claims}


def read_latest_draft(
    work_dir: Union[str, Path],
    preferred: str = "final.md",
    *,
    fallback_names: tuple = ("draft_v2.md", "draft_v1.md"),
) -> str:
    """
    Read the latest available draft, falling back through preferred -> fallback_names in order.

    Args:
        work_dir: Directory containing run artifacts.
        preferred: Preferred draft filename to try first (default "final.md").
        fallback_names: Filenames to try in order if preferred is missing/empty
            (default ("draft_v2.md", "draft_v1.md")).

    Returns:
        The content of the first existing, non-empty artifact among
        [preferred, *fallback_names], or "" if none are present.

    Preconditions:
        - ``work_dir`` is a directory path (existing or not); ``preferred`` and each
          entry in ``fallback_names`` are artifact filenames as accepted by ``read_artifact``.
    Postconditions:
        - Always returns a ``str`` (never ``None``); never raises for a missing file.
        - Tries ``preferred`` first, then each of ``fallback_names`` in order, returning
          the first non-empty result; returns ``""`` if all are missing or empty.
    """
    draft = read_artifact(work_dir, preferred, default="")
    if draft:
        return draft
    for name in fallback_names:
        draft = read_artifact(work_dir, name, default="")
        if draft:
            return draft
    return ""
