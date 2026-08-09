"""Regression: the graph sync worker (and graphiti_core) must stay out of the
module graph when ``NEO4J_BOLT_URL`` is unset.

``unified_api/main.py``'s ``lifespan()`` gates the
``agent_cognition.graph.sync_worker`` import behind
``shared.neo4j.is_neo4j_enabled()`` so unified-api pays zero import-time cost
for the Graphiti/Neo4j driver dependency chain when the knowledge-graph layer
is unused. Modeled on ``test_product_delivery_import_gating.py``'s subprocess
+ ``TestClient(app)`` lifespan-entry pattern: a fresh interpreter is required
because other test modules in this suite already import ``unified_api.main``,
populating ``sys.modules`` for the rest of the pytest process.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_AGENTS_ROOT = _BACKEND_ROOT / "agents"


def _subprocess_pythonpath() -> str:
    """Build PYTHONPATH matching ``backend/pytest.ini`` (``agents`` + backend root).

    Preconditions: ``_BACKEND_ROOT`` and ``_AGENTS_ROOT`` exist.
    Postconditions: returned string puts the ``agents`` and ``backend`` roots on
        ``sys.path`` (so ``unified_api`` and ``agent_cognition`` resolve).
    """
    assert _BACKEND_ROOT.is_dir()
    assert _AGENTS_ROOT.is_dir()
    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(_AGENTS_ROOT), str(_BACKEND_ROOT)]
    if existing:
        parts.append(existing)
    return os.pathsep.join(parts)


def _run(script: str, env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": _subprocess_pythonpath(), **env_overrides}
    return subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False)


def test_graph_sync_worker_not_imported_when_neo4j_disabled() -> None:
    """``NEO4J_BOLT_URL`` unset must keep the sync worker (and graphiti_core) out
    of ``sys.modules`` through a full ASGI lifespan run, not just at module import.

    Preconditions: a fresh interpreter whose PYTHONPATH includes ``backend`` + ``agents``.
    Postconditions: subprocess exits 0; assertions inside the child pass.
    """
    script = """
import sys
import unified_api.main

from fastapi.testclient import TestClient

with TestClient(unified_api.main.app):
    pass

assert "agent_cognition.graph.sync_worker" not in sys.modules, (
    "disabled Neo4j: agent_cognition.graph.sync_worker was still imported during lifespan startup"
)
assert "graphiti_core" not in sys.modules, (
    "disabled Neo4j: graphiti_core was still imported during lifespan startup"
)
print("ok")
"""
    result = _run(script, {"NEO4J_BOLT_URL": ""})
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ok" in result.stdout


def test_graph_sync_worker_imported_when_neo4j_enabled() -> None:
    """``NEO4J_BOLT_URL`` set must preserve today's behavior: the sync worker
    module is imported and the background task scheduled during lifespan startup.

    Preconditions: a fresh interpreter whose PYTHONPATH includes ``backend`` + ``agents``.
    Postconditions: subprocess exits 0; assertions inside the child pass.
    """
    script = """
import sys
import unified_api.main

from fastapi.testclient import TestClient

with TestClient(unified_api.main.app):
    pass

assert "agent_cognition.graph.sync_worker" in sys.modules, (
    "enabled Neo4j: agent_cognition.graph.sync_worker was not imported during lifespan startup"
)
print("ok")
"""
    result = _run(script, {"NEO4J_BOLT_URL": "bolt://neo4j:7687", "POSTGRES_HOST": ""})
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ok" in result.stdout
