"""Unit tests for the invoke shim's entrypoint dispatch logic."""

from __future__ import annotations

import sys
import types

import pytest

from shared.agent_invoke import dispatch
from shared.agent_invoke.dispatch import AgentNotRunnableError, invoke_entrypoint


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
async def test_forwards_agent_id_when_entrypoint_declares_it() -> None:
    seen: dict[str, object] = {}

    def handler(body, *, agent_id=None):
        seen["agent_id"] = agent_id
        return {"body": body, "agent_id": agent_id}

    _make_module("_si_test_agent_id", handler=handler)
    try:
        out = await invoke_entrypoint("_si_test_agent_id:handler", {"x": 1}, agent_id="route.id")
    finally:
        del sys.modules["_si_test_agent_id"]
    assert out == {"body": {"x": 1}, "agent_id": "route.id"}
    assert seen["agent_id"] == "route.id"


@pytest.mark.asyncio
async def test_forwards_agent_id_via_kwargs_catch_all() -> None:
    def handler(body, **kwargs):
        return {"kwargs": kwargs}

    _make_module("_si_test_kwargs", handler=handler)
    try:
        out = await invoke_entrypoint("_si_test_kwargs:handler", {"x": 1}, agent_id="route.id")
    finally:
        del sys.modules["_si_test_kwargs"]
    assert out == {"kwargs": {"agent_id": "route.id"}}


@pytest.mark.asyncio
async def test_omits_agent_id_when_entrypoint_does_not_accept_it() -> None:
    # A body-only entrypoint must still be called as ``fn(body)`` even when a route
    # agent_id is available — no unexpected-keyword TypeError.
    def handler(body):
        return {"echoed": body}

    _make_module("_si_test_bodyonly", handler=handler)
    try:
        out = await invoke_entrypoint("_si_test_bodyonly:handler", {"x": 9}, agent_id="route.id")
    finally:
        del sys.modules["_si_test_bodyonly"]
    assert out == {"echoed": {"x": 9}}


@pytest.mark.asyncio
async def test_agent_id_none_never_forwarded() -> None:
    # Default (no agent_id threaded): a declaring entrypoint sees its own default.
    def handler(body, *, agent_id="unset"):
        return {"agent_id": agent_id}

    _make_module("_si_test_default_id", handler=handler)
    try:
        out = await invoke_entrypoint("_si_test_default_id:handler", {"x": 1})
    finally:
        del sys.modules["_si_test_default_id"]
    assert out == {"agent_id": "unset"}


def test_accepts_kwarg_degrades_when_signature_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A callable whose signature can't be introspected (some C builtins) must not be
    # handed an unexpected keyword — degrade to False rather than raising.
    def _boom(_obj):
        raise ValueError("no signature found")

    monkeypatch.setattr(dispatch.inspect, "signature", _boom)
    assert dispatch._accepts_kwarg(lambda body: None, "agent_id") is False


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
