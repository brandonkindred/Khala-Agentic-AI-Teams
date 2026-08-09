"""Regression: importing ``api.main`` must not perform I/O.

The retroactive team-provisioning/registry-registration loop and the orphaned
pipeline-run reap used to run unconditionally at module import time (real
``AgenticTeamStore.list_teams()`` / ``PipelineRunner.reap_orphaned_runs()``
calls, among others), which made importing ``main.py`` unsafe for tests or
tooling. They now live in ``initialize_service()``, called only from the
``_startup`` lifespan hook.

Because other test modules in this suite already ``import
agent_team_studio.agentic_team_provisioning.api.main`` (populating
``sys.modules`` for the rest of the pytest process, and running the real
module-level code before this test's monkeypatches could ever attach), the
only reliable way to observe import-time behavior is a fresh subprocess — the
same pattern used by
``backend/unified_api/tests/test_product_delivery_import_gating.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parents[4]
_AGENTS_ROOT = _BACKEND_ROOT / "agents"


def _subprocess_pythonpath() -> str:
    """Build PYTHONPATH matching ``backend/pytest.ini`` (``agents`` + backend root).

    Preconditions: ``_BACKEND_ROOT`` and ``_AGENTS_ROOT`` exist.
    Postconditions: returned string puts the ``agents`` and ``backend`` roots on
        ``sys.path`` (so ``agent_team_studio`` and ``shared`` resolve).
    """
    assert _BACKEND_ROOT.is_dir()
    assert _AGENTS_ROOT.is_dir()
    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(_AGENTS_ROOT), str(_BACKEND_ROOT)]
    if existing:
        parts.append(existing)
    return os.pathsep.join(parts)


def _run(script: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": _subprocess_pythonpath()}
    # Deterministic: no live sweeper thread / no accidental real DB reachability
    # regardless of the ambient shell's configuration.
    env.pop("POSTGRES_HOST", None)
    return subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.slow_subprocess
def test_import_does_not_run_retroactive_provisioning_or_reap() -> None:
    """``import api.main`` alone must not call ``list_teams``/``reap_orphaned_runs``;
    entering the app's ASGI lifespan must.

    Preconditions: a fresh interpreter whose PYTHONPATH includes ``backend`` +
        ``agents``, with ``AgenticTeamStore.list_teams`` and
        ``PipelineRunner.reap_orphaned_runs`` patched to record-only stand-ins
        *before* ``api.main`` is ever imported.
    Postconditions: subprocess exits 0; the assertions inside the child (import
        performs zero calls, entering the lifespan performs exactly one call to
        each) all pass.
    """
    script = """
import agent_team_studio.agentic_team_provisioning.assistant.store as store_mod
import agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner as pr_mod

calls = []
store_mod.AgenticTeamStore.list_teams = lambda self: (calls.append("list_teams"), [])[1]
pr_mod.PipelineRunner.reap_orphaned_runs = lambda self: (calls.append("reap"), 0)[1]

import agent_team_studio.agentic_team_provisioning.api.main as main

assert calls == [], f"initialize_service work ran during import: {calls}"

from fastapi.testclient import TestClient

with TestClient(main.app):
    pass

assert calls == ["list_teams", "reap"], (
    f"expected exactly one retroactive-provisioning pass and one reap during "
    f"lifespan startup, got: {calls}"
)
print("ok")
"""
    result = _run(script)
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ok" in result.stdout
