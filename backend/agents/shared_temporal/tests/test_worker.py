"""Regression test: the workflow sandbox must pass through boto3/strands.

Registering any team's workflow class forces Python to import that class's
ancestor packages first (e.g. importing
``software_engineering_team.code_review_agent.temporal.workflows`` executes
``code_review_agent/__init__.py``, which eagerly imports ``chunk_reviewer``
and ``false_positive_filter`` — both import ``strands`` at module scope).
``strands`` unconditionally imports its Bedrock model provider, which does a
top-level ``import boto3``, and botocore does not tolerate being re-executed
inside the sandbox's isolated module namespace: it fails deep inside
``botocore.compat``'s ``from urllib3 import exceptions``. This pins the fix —
``strands``/``boto3``/``botocore``/``urllib3`` (and the pre-existing
``pydantic``/``pydantic_core``) must be configured as sandbox passthrough
modules so they load via the real importer instead of being replayed.
"""

from __future__ import annotations

import shared_temporal.worker as worker


def test_workflow_runner_passes_through_boto3_and_strands():
    runner = worker._build_workflow_runner()
    passthrough = runner.restrictions.passthrough_modules

    for module in ("pydantic", "pydantic_core", "strands", "boto3", "botocore", "urllib3"):
        assert module in passthrough, f"{module!r} must be a sandbox passthrough module"
