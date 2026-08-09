"""Regression test: the workflow sandbox must pass through boto3/strands/httpx.

Registering any team's workflow class forces Python to import that class's
ancestor packages first (e.g. importing
``software_engineering_team.code_review_agent.temporal.workflows`` executes
``code_review_agent/__init__.py``, and several other teams' top-level
``__init__.py`` eagerly imports agent/orchestrator code the same way).
``strands`` unconditionally imports its Bedrock model provider, which does a
top-level ``import boto3``, and botocore does not tolerate being re-executed
inside the sandbox's isolated module namespace: it fails deep inside
``botocore.compat``'s ``from urllib3 import exceptions``. ``llm_service``'s
Ollama client imports ``httpx`` at module scope, and ``httpx._models`` defines
``class _CookieCompatRequest(urllib.request.Request)`` at import time, which
raises ``RestrictedWorkflowAccessError`` on the sandbox-restricted
``urllib.request.Request``. This pins the fix — ``strands``/``boto3``/
``botocore``/``urllib3``/``httpx`` (and the pre-existing ``pydantic``/
``pydantic_core``) must be configured as sandbox passthrough modules so they
load via the real importer instead of being replayed.
"""

from __future__ import annotations

import shared.temporal.worker as worker


def test_workflow_runner_passes_through_boto3_strands_and_httpx():
    runner = worker._build_workflow_runner()
    passthrough = runner.restrictions.passthrough_modules

    for module in (
        "pydantic",
        "pydantic_core",
        "strands",
        "boto3",
        "botocore",
        "urllib3",
        "httpx",
        "numpy",
        "pandas",
        "investment_team.market_data_service",
        "investment_team.strategy_lab.budget_config",
    ):
        assert module in passthrough, f"{module!r} must be a sandbox passthrough module"
