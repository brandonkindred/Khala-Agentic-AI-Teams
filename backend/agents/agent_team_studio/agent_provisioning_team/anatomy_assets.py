"""
Canonical agent anatomy: paths, loading, and workspace materialization.

Used by prompts (LLM context) and the documentation phase (on-disk bundle).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import List, Optional

PACKAGE_DIR = Path(__file__).resolve().parent
AGENT_ANATOMY_MD = PACKAGE_DIR / "AGENT_ANATOMY.md"
DESIGN_ASSETS_DIR = PACKAGE_DIR / "design_assets"

_anatomy_text_cache: Optional[str] = None


def load_agent_anatomy_text() -> str:
    """Full text of AGENT_ANATOMY.md for injection into prompts and docs."""
    global _anatomy_text_cache
    if _anatomy_text_cache is None:
        if AGENT_ANATOMY_MD.is_file():
            _anatomy_text_cache = AGENT_ANATOMY_MD.read_text(encoding="utf-8")
        else:
            _anatomy_text_cache = (
                f"(Missing file: {AGENT_ANATOMY_MD}. Restore AGENT_ANATOMY.md next to this module.)"
            )
    return _anatomy_text_cache


def list_design_asset_paths() -> List[Path]:
    """PNG paths under design_assets/, sorted by name."""
    if not DESIGN_ASSETS_DIR.is_dir():
        return []
    return sorted(DESIGN_ASSETS_DIR.glob("*.png"))


def get_anatomy_prompt_preamble() -> str:
    """
    Text to prepend to any LLM prompt that creates or refines an AI agent.

    Embeds the full AGENT_ANATOMY.md and lists diagram filenames for multimodal
    or human follow-up.
    """
    spec = load_agent_anatomy_text()
    names = [p.name for p in list_design_asset_paths()]
    diagram_block = (
        "\n".join(f"- {n}" for n in names)
        if names
        else "- (No PNG files found under design_assets/; add the canonical diagrams.)"
    )
    return f"""You MUST align all outputs with the Khala Agent Provisioning canonical agent anatomy below.
When creating or refining an AI agent, explicitly address: Input/Output, Agent core, Tools, Memory tiers,
Prompt roles (System/User/Assistant), Security Guardrails, and Subagents (recursive INPUT/OUTPUT).

## AGENT_ANATOMY.md (authoritative)

{spec}

## Reference diagram files (same content as design_assets/*.png in the repo)

{diagram_block}
"""


def copy_anatomy_bundle_to_directory(dest: Path) -> List[Path]:
    """
    Copy AGENT_ANATOMY.md and all design_assets/*.png into dest.

    Returns paths written (may be empty if source files are missing).
    """
    dest.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    if AGENT_ANATOMY_MD.is_file():
        out = dest / AGENT_ANATOMY_MD.name
        shutil.copy2(AGENT_ANATOMY_MD, out)
        written.append(out)
    for png in list_design_asset_paths():
        target = dest / png.name
        shutil.copy2(png, target)
        written.append(target)
    return written


def try_materialize_anatomy_bundle(workspace_path: str) -> Optional[str]:
    """
    Copy anatomy markdown + diagrams under workspace_path/docs/agent_anatomy/.

    Returns the directory path if at least one file was written, else None.
    Skips quietly when the workspace root cannot be created or is invalid.
    """
    root = Path(workspace_path)
    if not workspace_path or workspace_path in (".", "/"):
        return None
    try:
        bundle_dir = root / "docs" / "agent_anatomy"
        bundle_dir.parent.mkdir(parents=True, exist_ok=True)
        written = copy_anatomy_bundle_to_directory(bundle_dir)
        return str(bundle_dir) if written else None
    except OSError:
        return None
