"""Regression: ``agent_cognition.rules`` package init must not pull reflection/LLM."""

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


def test_import_rules_package_does_not_load_reflection_or_llm_service() -> None:
    """``import agent_cognition.rules`` must leave reflection and llm_service unloaded.

    Call sites that need reflection import ``agent_cognition.rules.reflection``
    (or its symbols) explicitly; the package init must not eagerly re-export them.

    Preconditions: a fresh interpreter whose PYTHONPATH includes ``backend`` + ``agents``.
    Postconditions: subprocess exits 0; assertion inside the child passes.
    """
    script = """
import sys
assert "agent_cognition.rules.reflection" not in sys.modules, (
    "reflection already loaded before import"
)
assert "llm_service" not in sys.modules, "llm_service already loaded before import"
import agent_cognition.rules  # noqa: F401
assert "agent_cognition.rules.reflection" not in sys.modules, (
    f"importing agent_cognition.rules loaded reflection: "
    f"{[m for m in sys.modules if m.startswith('agent_cognition.rules.reflection')]}"
)
assert not any(
    m == "llm_service" or m.startswith("llm_service.") for m in sys.modules
), (
    f"importing agent_cognition.rules loaded llm_service: "
    f"{[m for m in sys.modules if m == 'llm_service' or m.startswith('llm_service.')]}"
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
