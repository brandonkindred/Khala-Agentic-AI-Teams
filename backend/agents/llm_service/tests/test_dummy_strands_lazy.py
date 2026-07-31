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
    Postconditions: returned string puts the ``agents`` and ``backend`` roots on
        ``sys.path`` (so ``llm_service`` and ``shared`` resolve).
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
# Plain LLMClient construction/use must still leave Strands unloaded, and Agent-facing
# Model members (stateful) must be available without inheritance.
client = dummy.DummyLLMClient()
assert client.stateful is False
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


def test_dummy_attaches_real_strands_model_base_when_strands_path_used() -> None:
    """Strands ``Model`` real inheritance remains available when a Strands path is used.

    Preconditions: ``strands`` is importable.
    Postconditions: after ``ensure_strands_model_registration``, ``DummyLLMClient``
    has ``Model`` in its MRO (not merely a virtual ABC registration), and
    ``Agent(model=DummyLLMClient())`` can read ``stateful`` at construction.
    """
    pytest.importorskip("strands")
    from strands import Agent
    from strands.models.model import Model

    from llm_service.clients.dummy import DummyLLMClient, ensure_strands_model_registration

    ensure_strands_model_registration()
    assert Model in DummyLLMClient.__mro__
    client = DummyLLMClient()
    assert isinstance(client, Model)
    assert client.stateful is False
    client.update_config(model_id="dummy")
    assert client.get_config()["model_id"] == "dummy"
    # Agent reads model.stateful before any stream/update_config call.
    agent = Agent(model=client)
    assert agent.model is client


def test_ensure_invalidates_abc_negative_cache_for_pre_strands_instances() -> None:
    """Instances built before Strands import must become ``isinstance(..., Model)``.

    Preconditions: a fresh interpreter (so DummyLLMClient is constructed with no
    prior ``isinstance(..., Model)`` on a live Model).
    Postconditions: after ``ensure_strands_model_registration``, both
    ``isinstance`` and ``issubclass`` succeed despite a negative ABC cache from
    checks performed before ``__bases__`` mutation.
    """
    script = """
from llm_service.clients.dummy import DummyLLMClient, ensure_strands_model_registration

client = DummyLLMClient()
from strands.models.model import Model

# Pollute ABCMeta's negative cache while Model is still absent from the MRO.
assert isinstance(client, Model) is False
assert issubclass(DummyLLMClient, Model) is False

ensure_strands_model_registration()
assert Model in DummyLLMClient.__mro__
assert isinstance(client, Model) is True, "negative ABC cache survived __bases__ mutation"
assert issubclass(DummyLLMClient, Model) is True
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


def test_resolve_strands_model_returns_cold_dummy_unchanged() -> None:
    """Cold-constructed DummyLLMClient must short-circuit in ``resolve_strands_model``.

    Preconditions: a fresh interpreter that constructs Dummy before importing
        ``resolve_strands_model`` (which loads Strands).
    Postconditions: resolver returns the same instance (Model short-circuit), not
        an ``LLMClientModel`` wrapper.
    """
    script = """
from llm_service.clients.dummy import DummyLLMClient

client = DummyLLMClient()
from llm_service.strands_model import resolve_strands_model
from strands.models.model import Model

resolved = resolve_strands_model(client, response_format="text")
assert resolved is client, f"expected Dummy short-circuit, got {type(resolved)!r}"
assert isinstance(client, Model) is True
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
