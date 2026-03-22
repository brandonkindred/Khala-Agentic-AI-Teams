#!/usr/bin/env python3
"""Bedrock AgentCore entry point for Enterprise Architect Agent System.

Deploys the orchestrator as an HTTP service for Bedrock AgentCore Runtime.
Supports up to 8-hour task execution for long architecture reviews.

Usage:
    python agentcore_main.py

Test locally:
    curl -X POST http://localhost:8080/invocations \\
      -H "Content-Type: application/json" \\
      -d '{"spec": "# My Project Spec\\n\\nBuild a web app..."}'
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents.orchestrator import create_orchestrator  # noqa: E402
from agents.session import get_session_manager  # noqa: E402


def _create_app():
    """Create the Bedrock AgentCore app with orchestrator entrypoint."""
    try:
        from bedrock_agentcore.runtime import BedrockAgentCoreApp
    except ImportError as e:
        raise ImportError(
            "bedrock-agentcore is required for AgentCore deployment. "
            "Install with: pip install bedrock-agentcore"
        ) from e

    app = BedrockAgentCoreApp()
    session_manager = get_session_manager()
    orchestrator = create_orchestrator(session_manager=session_manager)

    @app.entrypoint
    def invoke(payload: dict) -> dict:
        """Process architecture spec and return results.

        Payload:
            spec: Required. The specification/planning document content.
            session_id: Optional. Session ID for resume/audit.
            output_dir: Optional. Override output directory.

        Returns:
            result: Orchestrator response message.
            outputs_path: Path to generated architecture package.
        """
        spec = payload.get("spec", "")
        if not spec:
            return {
                "error": "Missing 'spec' in payload. Provide specification content.",
                "result": None,
                "outputs_path": None,
            }
        result = orchestrator(spec)
        output_dir = payload.get("output_dir") or str(_ROOT / "outputs")
        return {
            "result": str(result),
            "outputs_path": output_dir,
        }

    return app


if __name__ == "__main__":
    app = _create_app()
    app.run()
