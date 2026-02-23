"""
Deliver phase: commit and finalize planning artifacts.

Roles: System Design, Architecture (High-Level).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from shared.llm import LLMClient

from ..models import DeliverPhaseResult, ImplementationPhaseResult

logger = logging.getLogger(__name__)


def run_deliver(
    llm: LLMClient,
    spec_content: str,
    repo_path: Path,
    implementation_result: Optional[ImplementationPhaseResult] = None,
) -> DeliverPhaseResult:
    """
    Run Deliver phase (roles: System Design, Architecture); finalize planning artifacts.
    Optionally commits via git if the repo is a git repository.
    """
    committed = False
    try:
        import subprocess
        if (repo_path / ".git").exists():
            subprocess.run(
                ["git", "add", "planning_v2/", "planning_v2/*"],
                cwd=repo_path,
                check=False,
                capture_output=True,
            )
            r = subprocess.run(
                ["git", "commit", "-m", "chore: planning-v2 artifacts"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            committed = r.returncode == 0
            if committed:
                logger.info("Deliver: committed planning artifacts")
    except Exception as e:
        logger.warning("Deliver git commit failed (non-fatal): %s", e)

    summary = "Planning-v2 artifacts delivered."
    if implementation_result and implementation_result.assets_created:
        summary = f"Deliver complete. Assets: {', '.join(implementation_result.assets_created)}."
    if committed:
        summary += " Changes committed."
    return DeliverPhaseResult(committed=committed, summary=summary)
