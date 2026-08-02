"""Regression tests for the package-level lazy Strands re-exports.

``llm_service/__init__.py`` documents (in the comment above its ``__getattr__``)
that ``LLMClientModel`` / ``run_json_via_strands`` (from ``.strands_adapter``)
and ``get_strands_model`` / ``_clear_strands_model_cache_for_testing`` (from
``.strands_provider``) are resolved lazily via PEP 562 ``__getattr__`` so that
``import llm_service`` does not pull the optional ``strands-agents`` package
(and its ``botocore`` dependency) into ``sys.modules`` until a Strands code
path is actually exercised. These tests pin that contract directly against
the package ``__init__``, rather than only incidentally via tests of other
modules.
"""

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


def test_import_llm_service_does_not_load_strands() -> None:
    """Plain ``import llm_service`` must leave ``strands`` out of ``sys.modules``.

    Preconditions: a fresh interpreter whose PYTHONPATH includes ``backend`` + ``agents``.
    Postconditions: subprocess exits 0; ``strands`` never appears in ``sys.modules``
        merely from importing the package (no eager import of ``get_strands_model``
        or the other Strands-only names survives in ``__init__.py``).
    """
    script = """
import sys
assert "strands" not in sys.modules, "strands already loaded before import"
import llm_service
assert "strands" not in sys.modules, (
    f"importing llm_service loaded strands: "
    f"{[m for m in sys.modules if m == 'strands' or m.startswith('strands.')]}"
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
        f"subprocess failed (rc={result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ok" in result.stdout


def test_getattr_unknown_attribute_raises() -> None:
    """Accessing a name outside both lazy-export sets raises ``AttributeError``.

    Preconditions: none.
    Postconditions: the raised error names both the module and the missing attribute,
        matching the fallback branch of ``llm_service.__getattr__``.
    """
    import llm_service

    with pytest.raises(AttributeError, match="llm_service.*totally_not_a_real_export"):
        llm_service.totally_not_a_real_export


@pytest.mark.parametrize(
    ("lazy_name", "source_module"),
    [
        ("get_strands_model", "strands_provider"),
        ("LLMClientModel", "strands_adapter"),
    ],
)
def test_getattr_lazily_resolves_and_caches(lazy_name: str, source_module: str) -> None:
    """A lazy export is resolved from its owning submodule on first access, not
    eagerly imported at module load time, and is cached after that first access.

    Run in a fresh subprocess (rather than in-process) so the "before first access"
    assertion is never polluted by another test file having already triggered
    ``__getattr__`` for the same name earlier in the same pytest session.

    Preconditions: the optional ``strands-agents`` package is importable.
    Postconditions:
    - Before first access, ``lazy_name`` is absent from ``vars(llm_service)``.
    - The resolved attribute is the identical object as ``<source_module>.<lazy_name>``,
      proving ``__getattr__`` (not a separate eager import) supplied it.
    - After first access, ``__getattr__``'s ``globals()[name] = value`` caching makes
      ``lazy_name`` present in ``vars(llm_service)``, so a second access is a plain
      attribute lookup rather than a repeated ``__getattr__`` call.
    """
    pytest.importorskip("strands")
    script = f"""
import llm_service
import llm_service.{source_module} as source_module

assert {lazy_name!r} not in vars(llm_service), "eagerly imported before first access"

resolved = llm_service.{lazy_name}

assert resolved is source_module.{lazy_name}, "not resolved from its owning submodule"
assert {lazy_name!r} in vars(llm_service), "__getattr__ did not cache into globals()"
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
        f"subprocess failed (rc={result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ok" in result.stdout
