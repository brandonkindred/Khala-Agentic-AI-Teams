"""Integration point for software engineering team orchestrator.

When SW_USE_ENTERPRISE_ARCHITECT=true, the software engineering team
orchestrator can call run_enterprise_architect() to get an enterprise-grade
architecture package. The architecture-overview.md can be used to enrich
the ArchitectureExpertAgent input or as supplementary context.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def run_enterprise_architect(
    spec_content: str,
    output_dir: str | Path | None = None,
    work_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run the Enterprise Architect orchestrator and return results.

    Called by the software engineering team when SW_USE_ENTERPRISE_ARCHITECT=true.
    Runs architect-agents as a subprocess to produce the full architecture package.

    Args:
        spec_content: The product specification and planning docs.
        output_dir: Override output directory. Default: work_path/architect-outputs
            or architect-agents/outputs.
        work_path: Working directory (e.g. job work path). Used to resolve output_dir.

    Returns:
        dict with:
            - success: bool
            - architecture_overview: str (content of architecture-overview.md)
            - outputs_path: str (path to outputs directory)
            - error: str | None (error message if success=False)
    """
    root = Path(__file__).resolve().parent
    main_py = root / "main.py"
    if not main_py.exists():
        return {
            "success": False,
            "architecture_overview": "",
            "outputs_path": "",
            "error": f"architect-agents main.py not found at {main_py}",
        }

    out = output_dir or (Path(work_path) / "architect-outputs" if work_path else root / "outputs")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["ARCHITECT_OUTPUT_DIR"] = str(out)
    env["ARCHITECT_SESSION_DISABLED"] = "1"  # Disable session when called as subprocess

    try:
        result = subprocess.run(
            [sys.executable, str(main_py)],
            input=spec_content,
            capture_output=True,
            text=True,
            cwd=str(root),
            env=env,
            timeout=3600,  # 1 hour max
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "architecture_overview": "",
            "outputs_path": str(out),
            "error": "Enterprise architect timed out after 1 hour",
        }
    except Exception as e:
        return {
            "success": False,
            "architecture_overview": "",
            "outputs_path": str(out),
            "error": str(e),
        }

    overview_path = out / "architecture-overview.md"
    overview = ""
    if overview_path.exists():
        overview = overview_path.read_text(encoding="utf-8")

    return {
        "success": result.returncode == 0,
        "architecture_overview": overview,
        "outputs_path": str(out),
        "error": result.stderr.strip() if result.returncode != 0 else None,
    }
