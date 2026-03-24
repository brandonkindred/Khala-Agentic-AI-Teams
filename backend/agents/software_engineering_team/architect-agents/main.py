#!/usr/bin/env python3
"""Entry point for Enterprise Architect Agent System.

Load spec and planning docs, kick off the orchestrator, and produce
the architecture package in outputs/.

Usage:
    python main.py [spec_path]
    python main.py                    # reads from stdin
    python main.py ./my-spec.md       # reads from file

Environment:
    ARCHITECT_OUTPUT_DIR      Output directory (default: outputs)
    ARCHITECT_SESSION_BUCKET  S3 bucket for session persistence (optional)
    ARCHITECT_SESSION_DISABLED=1  Disable session persistence
    ARCHITECT_MODEL_ORCHESTRATOR   Bedrock model for orchestrator
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure architect-agents root is on path
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents.orchestrator import create_orchestrator  # noqa: E402
from agents.session import get_session_manager  # noqa: E402


def main() -> int:
    """Load spec, run orchestrator, produce outputs."""
    if len(sys.argv) > 1:
        spec_path = Path(sys.argv[1])
        if not spec_path.exists():
            print(f"Error: Spec file not found: {spec_path}", file=sys.stderr)
            return 1
        spec_content = spec_path.read_text(encoding="utf-8")
    else:
        spec_content = sys.stdin.read()
        if not spec_content.strip():
            print(
                "Error: No spec provided. Pass a file path or pipe spec to stdin.", file=sys.stderr
            )
            return 1

    output_dir = Path(
        os.environ.get("ARCHITECT_OUTPUT_DIR", str(Path(__file__).resolve().parent / "outputs"))
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    session_manager = get_session_manager()
    orchestrator = create_orchestrator(session_manager=session_manager)

    prompt = f"""Analyze this specification and produce a complete architecture package.

## Specification

{spec_content}

## Instructions

1. Use file_read_tool if you need to load additional planning docs referenced in the spec.
2. Delegate to specialists in order: application_architect + data_architect (parallel), then cloud_infrastructure_architect + security_architect (parallel), then observability_architect.
3. Synthesize all outputs and use document_writer_tool to write the full deliverable set to the outputs/ directory:
   - architecture-overview.md
   - adr/ (one ADR per significant decision)
   - diagrams/ (Mermaid specs)
   - technology-selections.md
   - cost-estimate.md
   - security-requirements.md
   - data-architecture.md
   - observability-plan.md
   - open-questions.md

Use output_dir="{output_dir}" in every document_writer_tool call.
"""

    print("Running Enterprise Architect Orchestrator...", file=sys.stderr)
    result = orchestrator(prompt)
    print(str(result), file=sys.stdout)
    print(f"\nOutputs written to {output_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
