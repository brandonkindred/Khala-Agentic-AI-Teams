"""Regression: importing reflection/rollup must not pull in ``llm_service``."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_AGENTS_ROOT = _BACKEND_ROOT / "agents"


def _subprocess_pythonpath() -> str:
    """Build PYTHONPATH matching pytest.ini (``agents`` + backend root).

    Preconditions: ``_BACKEND_ROOT`` and ``_AGENTS_ROOT`` exist.
    Postconditions: returned string puts the ``agents`` and ``backend`` roots on
        ``sys.path`` (so ``agent_cognition`` and ``llm_service`` resolve).
    """
    assert _BACKEND_ROOT.is_dir()
    assert _AGENTS_ROOT.is_dir()
    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(_AGENTS_ROOT), str(_BACKEND_ROOT)]
    if existing:
        parts.append(existing)
    return os.pathsep.join(parts)


def _assert_import_leaves_llm_service_unloaded(module_name: str) -> None:
    """Run a child interpreter that imports ``module_name`` without loading llm_service.

    Preconditions: ``module_name`` is a non-empty dotted import path.
    Postconditions: child exits 0 and prints ``ok``; raises ``AssertionError`` otherwise.
    """
    assert module_name and isinstance(module_name, str)
    script = f"""
import sys
assert "llm_service" not in sys.modules, "llm_service already loaded before import"
assert not any(
    m == "llm_service" or m.startswith("llm_service.") for m in sys.modules
), "llm_service already loaded before import"
import {module_name}  # noqa: F401
assert not any(
    m == "llm_service" or m.startswith("llm_service.") for m in sys.modules
), (
    f"importing {module_name} loaded llm_service: "
    f"{{[m for m in sys.modules if m == 'llm_service' or m.startswith('llm_service.')]}}"
)
print("ok")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": _subprocess_pythonpath()},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ok" in result.stdout


def test_import_reflection_does_not_load_llm_service() -> None:
    """``import agent_cognition.rules.reflection`` must leave llm_service unloaded.

    Reflection obtains an LLM client only when ``reflect`` (or a lazy facade)
    actually runs; merely importing the module must stay free of ``llm_service``.

    Preconditions: a fresh interpreter whose PYTHONPATH includes ``backend`` + ``agents``.
    Postconditions: subprocess exits 0; assertion inside the child passes.
    """
    _assert_import_leaves_llm_service_unloaded("agent_cognition.rules.reflection")


def test_import_rollup_does_not_load_llm_service() -> None:
    """``import agent_cognition.memory.rollup`` must leave llm_service unloaded.

    Rollup obtains an LLM client only when ``ensure_rollups_current`` (or a lazy
    facade) actually runs; merely importing the module must stay free of
    ``llm_service``.

    Preconditions: a fresh interpreter whose PYTHONPATH includes ``backend`` + ``agents``.
    Postconditions: subprocess exits 0; assertion inside the child passes.
    """
    _assert_import_leaves_llm_service_unloaded("agent_cognition.memory.rollup")
