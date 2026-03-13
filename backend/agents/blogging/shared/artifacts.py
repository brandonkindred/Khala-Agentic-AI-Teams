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
    "brand_spec.yaml",
    "content_brief.md",
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
)

# Static metadata: which pipeline phase/agent produces each artifact (for API list response)
ARTIFACT_PRODUCER: dict[str, dict[str, str]] = {
    "brand_spec.yaml": {"producer_phase": "draft_initial", "producer_agent": "Pipeline (brand load)"},
    "content_brief.md": {"producer_phase": "review", "producer_agent": "BlogReviewAgent"},
    "research_packet.md": {"producer_phase": "research", "producer_agent": "ResearchAgent"},
    "allowed_claims.json": {"producer_phase": "research", "producer_agent": "ResearchAgent"},
    "outline.md": {"producer_phase": "review", "producer_agent": "BlogReviewAgent"},
    "draft_v1.md": {"producer_phase": "draft_initial", "producer_agent": "BlogDraftAgent"},
    "draft_v2.md": {"producer_phase": "copy_edit", "producer_agent": "BlogCopyEditorAgent"},
    "final.md": {"producer_phase": "finalize", "producer_agent": "BlogCopyEditorAgent"},
    "compliance_report.json": {"producer_phase": "compliance", "producer_agent": "BlogComplianceAgent"},
    "fact_check_report.json": {"producer_phase": "fact_check", "producer_agent": "BlogFactCheckAgent"},
    "validator_report.json": {"producer_phase": "compliance", "producer_agent": "Validators"},
    "publishing_pack.json": {"producer_phase": "finalize", "producer_agent": "Pipeline"},
    "editor_feedback.json": {"producer_phase": "copy_edit", "producer_agent": "BlogCopyEditorAgent"},
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
