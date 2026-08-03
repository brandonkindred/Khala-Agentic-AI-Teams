"""Regression: the cognition + llm_service cold path must not pull in Strands/botocore.

Guards the import edge that used to run eagerly at module-import time:
``agent_cognition`` invoke-boundary modules -> reflection rules -> ``llm_service`` ->
``DummyLLMClient`` -> Strands ``Model`` (which itself pulls in ``botocore``/``boto3``
via ``strands.models.bedrock``). Both halves of the contract are asserted in one
subprocess: the cold path stays free of ``strands``/``botocore`` on plain import and
use, and explicitly exercising the Strands path still makes ``strands`` load.
"""

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


def test_cognition_and_dummy_cold_path_stay_free_of_strands_and_botocore() -> None:
    """Cold-path import/use of cognition + ``DummyLLMClient`` must not load Strands/botocore.

    Mirrors the dependency chain unified-api Agent Console routes exercise
    (``agent_cognition.invoke_gate`` + ``agent_cognition.rules``) plus direct use of
    ``llm_service.clients.dummy`` as a plain ``LLMClient``. Then explicitly triggers
    the Strands path and confirms ``strands`` loads on demand, proving the guard is
    "absent until requested," not "permanently broken."

    Preconditions: a fresh interpreter whose PYTHONPATH includes ``backend`` + ``agents``.
    Postconditions: subprocess exits 0; assertions inside the child pass.
    """
    script = """
import sys

def _loaded(prefix):
    return [m for m in sys.modules if m == prefix or m.startswith(prefix + ".")]

assert not _loaded("strands"), "strands already loaded before import"
assert not _loaded("botocore"), "botocore already loaded before import"

import agent_cognition.invoke_gate  # noqa: F401
import agent_cognition.rules  # noqa: F401
assert not _loaded("strands"), f"importing cognition loaded strands: {_loaded('strands')}"
assert not _loaded("botocore"), f"importing cognition loaded botocore: {_loaded('botocore')}"

import llm_service.clients.dummy as dummy
assert not _loaded("strands"), f"importing dummy loaded strands: {_loaded('strands')}"
assert not _loaded("botocore"), f"importing dummy loaded botocore: {_loaded('botocore')}"

client = dummy.DummyLLMClient()
assert client.stateful is False
_ = client.complete("hello", objective="test")
_ = client.complete_json("hello", objective="test")
assert not _loaded("strands"), f"using DummyLLMClient as LLMClient loaded strands: {_loaded('strands')}"
assert not _loaded("botocore"), f"using DummyLLMClient as LLMClient loaded botocore: {_loaded('botocore')}"

# Explicit Strands path: strands must load on demand, not stay permanently absent.
dummy.ensure_strands_model_registration()
assert _loaded("strands"), "explicit Strands path did not load strands"

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
