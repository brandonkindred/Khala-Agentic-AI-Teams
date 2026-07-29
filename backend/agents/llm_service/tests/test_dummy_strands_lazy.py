"""Regression: DummyLLMClient must not pull Strands in on plain import/use."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_AGENTS_ROOT = _BACKEND_ROOT / "agents"


def _subprocess_pythonpath() -> str:
    """Build PYTHONPATH matching pytest.ini (``agents`` + backend root).

    Preconditions: ``_BACKEND_ROOT`` and ``_AGENTS_ROOT`` exist.
    Postconditions: returned string puts ``shared`` and ``llm_service`` on sys.path.
    """
    assert _BACKEND_ROOT.is_dir()
    assert _AGENTS_ROOT.is_dir()
    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(_AGENTS_ROOT), str(_BACKEND_ROOT)]
    if existing:
        parts.append(existing)
    return os.pathsep.join(parts)


def test_import_dummy_module_does_not_load_strands() -> None:
    """``import llm_service.clients.dummy`` must leave ``strands`` out of ``sys.modules``.

    Preconditions: a fresh interpreter whose PYTHONPATH includes ``backend`` + ``agents``.
    Postconditions: subprocess exits 0; assertion inside the child passes.
    """
    script = """
import sys
assert "strands" not in sys.modules, "strands already loaded before import"
import llm_service.clients.dummy as dummy
assert "strands" not in sys.modules, (
    f"importing dummy loaded strands: "
    f"{[m for m in sys.modules if m == 'strands' or m.startswith('strands.')]}"
)
# Plain LLMClient construction/use must still leave Strands unloaded.
client = dummy.DummyLLMClient()
_ = client.complete("hello", objective="test")
_ = client.complete_json("hello", objective="test")
assert "strands" not in sys.modules, "using DummyLLMClient as LLMClient loaded strands"
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


def test_dummy_registers_as_strands_model_when_strands_path_used() -> None:
    """Strands ``Model`` registration remains available when a Strands path is used.

    Preconditions: ``strands`` is importable.
    Postconditions: after ``ensure_strands_model_registration``, ``DummyLLMClient``
    is a virtual subclass of ``strands.models.model.Model``.
    """
    pytest.importorskip("strands")
    from strands.models.model import Model

    from llm_service.clients.dummy import DummyLLMClient, ensure_strands_model_registration

    ensure_strands_model_registration()
    client = DummyLLMClient()
    assert isinstance(client, Model)
    client.update_config(model_id="dummy")
    assert client.get_config()["model_id"] == "dummy"
