"""
Tests that verify agents emit expected log messages.

Run with visible logs: pytest tests/test_logging.py -v --log-cli-level=INFO
"""

import logging

from architecture_expert import ArchitectureExpertAgent, ArchitectureInput

from llm_service import DummyLLMClient
from software_engineering_team.shared.models import ProductRequirements


def test_architecture_agent_logs_start_and_done(caplog) -> None:
    """Architecture agent logs 'starting' and 'done' at INFO level."""
    caplog.set_level(logging.INFO)
    llm = DummyLLMClient()
    agent = ArchitectureExpertAgent(llm_client=llm)
    reqs = ProductRequirements(
        title="Test",
        description="Desc",
        acceptance_criteria=[],
        constraints=[],
    )
    agent.run(ArchitectureInput(requirements=reqs))

    records = [r.message for r in caplog.records]
    assert any("starting" in r.lower() for r in records)
    assert any("done" in r.lower() for r in records)
    assert any("components" in r.lower() for r in records)
