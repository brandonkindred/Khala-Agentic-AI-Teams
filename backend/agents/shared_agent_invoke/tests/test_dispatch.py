"""Unit tests for the invoke shim's entrypoint dispatch logic."""

from __future__ import annotations

import sys
import types

import pytest

from shared_agent_invoke.dispatch import AgentNotRunnableError, invoke_entrypoint


def _make_module(name: str, **attrs: object) -> None:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


@pytest.mark.asyncio
async def test_dispatches_to_plain_function() -> None:
    def handler(body):
        return {"echoed": body}

    _make_module("_si_test_plain", handler=handler)
    try:
        out = await invoke_entrypoint("_si_test_plain:handler", {"x": 1})
    finally:
        del sys.modules["_si_test_plain"]
    assert out == {"echoed": {"x": 1}}


@pytest.mark.asyncio
async def test_dispatches_to_class_with_run_method() -> None:
    class Agent:
        def run(self, body):
            return {"ok": True, "body": body}

    _make_module("_si_test_class", Agent=Agent)
    try:
        out = await invoke_entrypoint("_si_test_class:Agent", {"x": 2})
    finally:
        del sys.modules["_si_test_class"]
    assert out == {"ok": True, "body": {"x": 2}}


@pytest.mark.asyncio
async def test_dispatches_to_factory_function_returning_agent() -> None:
    class Agent:
        def __call__(self, body):
            return {"called_with": body}

    def make_agent():
        return Agent()

    _make_module("_si_test_factory", make_agent=make_agent)
    try:
        out = await invoke_entrypoint("_si_test_factory:make_agent", {"y": 3})
    finally:
        del sys.modules["_si_test_factory"]
    assert out == {"called_with": {"y": 3}}


@pytest.mark.asyncio
async def test_dispatches_to_coroutine_function() -> None:
    async def handler(body):
        return {"async_echo": body}

    _make_module("_si_test_async", handler=handler)
    try:
        out = await invoke_entrypoint("_si_test_async:handler", {"a": 1})
    finally:
        del sys.modules["_si_test_async"]
    assert out == {"async_echo": {"a": 1}}


@pytest.mark.asyncio
async def test_malformed_entrypoint_raises_not_runnable() -> None:
    with pytest.raises(AgentNotRunnableError):
        await invoke_entrypoint("no_colon_here", {})


@pytest.mark.asyncio
async def test_missing_module_raises_not_runnable() -> None:
    with pytest.raises(AgentNotRunnableError):
        await invoke_entrypoint("does.not.exist:Symbol", {})


@pytest.mark.asyncio
async def test_missing_symbol_raises_not_runnable() -> None:
    _make_module("_si_test_missing", Other=object)
    try:
        with pytest.raises(AgentNotRunnableError):
            await invoke_entrypoint("_si_test_missing:NoSuch", {})
    finally:
        del sys.modules["_si_test_missing"]


@pytest.mark.asyncio
async def test_class_with_no_invoke_method_raises() -> None:
    class NoMethods:
        pass

    _make_module("_si_test_no_method", NoMethods=NoMethods)
    try:
        with pytest.raises(AgentNotRunnableError):
            await invoke_entrypoint("_si_test_no_method:NoMethods", {})
    finally:
        del sys.modules["_si_test_no_method"]


@pytest.mark.asyncio
async def test_class_requiring_constructor_args_raises() -> None:
    class NeedsArg:
        def __init__(self, required):
            self.required = required

        def run(self, body):
            return body

    _make_module("_si_test_needs_arg", NeedsArg=NeedsArg)
    try:
        with pytest.raises(AgentNotRunnableError):
            await invoke_entrypoint("_si_test_needs_arg:NeedsArg", {})
    finally:
        del sys.modules["_si_test_needs_arg"]
