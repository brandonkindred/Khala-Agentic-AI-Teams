"""Shared test-only scaffolding for driving a real ``temporalio`` sandbox.

Not part of ``shared.temporal``'s production public API (see
``shared/temporal/__init__.py``) -- this module is imported only from test
files, several of which drive an actual embedded Temporal test server via
``temporalio.testing.WorkflowEnvironment`` rather than a monkeypatched
``execute_activity``. Before this module existed, each such test file kept
its own byte-for-byte copy of the bootstrap below; this is the single shared
copy.
"""

from __future__ import annotations

import contextlib

import pytest
from temporalio.testing import WorkflowEnvironment


@contextlib.asynccontextmanager
async def workflow_environment():
    """Start a time-skipping ``WorkflowEnvironment`` with no worker attached.

    Preconditions:
        - Caller is an async test (or other async context) that will drive
          the yielded ``env`` and any workers itself.
    Postconditions:
        - Yields a started ``WorkflowEnvironment``. Skips the test (rather
          than failing) when the ephemeral Temporal test-server binary
          cannot be downloaded (offline CI). The environment is shut down on
          exit.
    """
    try:
        test_env = await WorkflowEnvironment.start_time_skipping()
    except RuntimeError as exc:
        pytest.skip(f"Temporal ephemeral test server unavailable (no egress?): {exc}")

    async with test_env as env:
        yield env
