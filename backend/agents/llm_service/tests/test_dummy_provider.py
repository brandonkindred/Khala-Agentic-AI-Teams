"""Direct tests for ``force_dummy_llm_provider``'s patch/restore behavior.

Complements the indirect coverage in ``branding_team/tests/test_agents.py``
(via the ``force_dummy_llm`` fixture) and
``branding_team/tests/test_eval_selective_context.py`` with focused
assertions on the context manager itself: it must patch every chokepoint
while active, and restore every one of them on exit -- including when the
wrapped block raises, and including the "was originally unset" case for
``LLM_PROVIDER``.
"""

from __future__ import annotations

import builtins
import os

import pytest

from llm_service import config as llm_config
from llm_service import provider_store as llm_provider_store
from llm_service.dummy_provider import force_dummy_llm_provider


def test_patches_runtime_provider_list_and_env_while_active() -> None:
    with force_dummy_llm_provider():
        assert os.environ.get("LLM_PROVIDER") == "dummy"
        assert llm_config._runtime("any-key") == ""
        assert llm_provider_store.load_ordered_entries() == []
        assert llm_config.resolve_provider() == "dummy"


def test_restores_runtime_and_provider_list_on_normal_exit() -> None:
    original_runtime = llm_config._runtime
    original_load_ordered_entries = llm_provider_store.load_ordered_entries

    with force_dummy_llm_provider():
        pass

    assert llm_config._runtime is original_runtime
    assert llm_provider_store.load_ordered_entries is original_load_ordered_entries


def test_restores_on_exception_inside_block() -> None:
    original_runtime = llm_config._runtime
    original_load_ordered_entries = llm_provider_store.load_ordered_entries

    with pytest.raises(RuntimeError):
        with force_dummy_llm_provider():
            assert llm_config._runtime("any-key") == ""
            raise RuntimeError("boom")

    assert llm_config._runtime is original_runtime
    assert llm_provider_store.load_ordered_entries is original_load_ordered_entries


def test_restores_llm_provider_env_when_originally_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "claude")

    with force_dummy_llm_provider():
        assert os.environ.get("LLM_PROVIDER") == "dummy"

    assert os.environ.get("LLM_PROVIDER") == "claude"


def test_restores_llm_provider_env_when_originally_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    with force_dummy_llm_provider():
        assert os.environ.get("LLM_PROVIDER") == "dummy"

    assert "LLM_PROVIDER" not in os.environ


def test_restores_llm_provider_env_on_exception_when_originally_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    with pytest.raises(RuntimeError):
        with force_dummy_llm_provider():
            raise RuntimeError("boom")

    assert "LLM_PROVIDER" not in os.environ


def test_still_forces_dummy_provider_when_strands_provider_is_unimportable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The optional ``strands-agents`` import must never block the core patches.

    Simulates an environment without the optional ``strands-agents`` package
    by making ``from .strands_provider import ...`` raise ``ImportError``:
    the context manager must still force the dummy provider, and must still
    restore every patch on exit -- proving the Strands cache-clear import is
    decoupled from (and cannot abort) the core ``_runtime``/
    ``load_ordered_entries``/``LLM_PROVIDER`` patches.

    The relative ``from .strands_provider import X`` inside
    ``force_dummy_llm_provider`` compiles to ``IMPORT_NAME`` with the
    argument ``"strands_provider"`` (unqualified) and ``level=1`` --
    confirmed via ``dis.dis`` -- not the fully-qualified
    ``"llm_service.strands_provider"``; Python only resolves the package
    prefix internally using ``level`` once inside the real ``__import__``.
    The fake below must match on the unqualified name, and asserts it was
    actually invoked so this test cannot pass vacuously in an environment
    that already lacks ``strands-agents`` (where the import would raise on
    its own, with or without patching).
    """
    original_runtime = llm_config._runtime
    original_load_ordered_entries = llm_provider_store.load_ordered_entries
    real_import = builtins.__import__
    intercepted = []

    def _fake_import(name, *args, **kwargs):
        if name == "strands_provider":
            intercepted.append(name)
            raise ImportError("simulated: strands-agents not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    with force_dummy_llm_provider():
        assert intercepted == ["strands_provider"]
        assert os.environ.get("LLM_PROVIDER") == "dummy"
        assert llm_config._runtime("any-key") == ""
        assert llm_provider_store.load_ordered_entries() == []

    assert llm_config._runtime is original_runtime
    assert llm_provider_store.load_ordered_entries is original_load_ordered_entries
