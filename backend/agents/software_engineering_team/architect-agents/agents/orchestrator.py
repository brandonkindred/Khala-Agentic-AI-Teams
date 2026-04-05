"""Enterprise Architect Orchestrator — Lead Agent that delegates to specialists."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from strands import Agent  # noqa: E402
from tools import (  # noqa: E402
    aws_pricing_tool,
    document_writer_tool,
    file_read_tool,
    web_search_tool,
)

from agents.api_design import api_design_architect  # noqa: E402
from agents.application import application_architect  # noqa: E402
from agents.architecture_scrutineer import architecture_scrutineer  # noqa: E402
from agents.cloud_infra import cloud_infrastructure_architect  # noqa: E402
from agents.data import data_architect  # noqa: E402
from agents.data_streaming import data_streaming_architect  # noqa: E402
from agents.devops import devops_architect  # noqa: E402
from agents.observability import observability_architect  # noqa: E402
from agents.security import security_architect  # noqa: E402

_PROMPT_PATH = _root / "prompts" / "orchestrator.md"


def create_orchestrator(session_manager=None):
    """Create the Enterprise Architect Orchestrator agent.

    Args:
        session_manager: Optional Strands SessionManager (S3SessionManager or
            FileSessionManager) for session persistence. If None, no persistence.

    Returns:
        Configured Agent instance.
    """
    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    model = os.environ.get(
        "ARCHITECT_MODEL_ORCHESTRATOR",
        "anthropic.claude-opus-4-6-v1",
    )
    tools = [
        file_read_tool,
        aws_pricing_tool,
        web_search_tool,
        document_writer_tool,
        # Phase 1: Security first
        security_architect,
        # Phase 2: Core design (parallel)
        application_architect,
        data_architect,
        api_design_architect,
        # Phase 3: Infrastructure & streaming (parallel)
        cloud_infrastructure_architect,
        data_streaming_architect,
        devops_architect,
        # Phase 4: Observability
        observability_architect,
        # Phase 5: Cross-review
        architecture_scrutineer,
    ]
    kwargs = {
        "model": model,
        "system_prompt": prompt,
        "tools": tools,
        "callback_handler": None,
    }
    if session_manager is not None:
        kwargs["session_manager"] = session_manager
    return Agent(**kwargs)
