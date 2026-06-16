"""Tests for the generic team-service entrypoint's wrapper-code generator.

These cover ``build_wrapper_body()`` — the per-worker ``_team_wrapper.py`` source
each uvicorn worker imports — without booting uvicorn. The entrypoint reads
``TEAM_MODULE``/``TEAM_NAME`` at import time (fail-fast in production), so they
are set before importing it here.
"""

from __future__ import annotations

import os

os.environ.setdefault("TEAM_MODULE", "coding_team.api.main")
os.environ.setdefault("TEAM_NAME", "coding_team")

import pytest  # noqa: E402
from team_service import entrypoint  # noqa: E402


def _compile(src: str) -> None:
    # Proves the generated wrapper is syntactically valid Python.
    compile(src, "<wrapper>", "exec")


def test_wrapper_body_compiles_and_defines_app() -> None:
    body = entrypoint.build_wrapper_body("coding_team", "coding_team.api.main", "app")
    _compile(body)
    assert "from coding_team.api.main import app as app" in body


def test_wrapper_arms_diagnostics_watchdog_and_instrumentation() -> None:
    body = entrypoint.build_wrapper_body("coding_team", "coding_team.api.main", "app")
    assert "install_fault_diagnostics()" in body
    assert "start_memory_watchdog('coding_team')" in body
    assert "instrument_fastapi_app(app, team_key='coding_team')" in body
    assert "endpoint='/metrics'" in body  # prometheus /metrics still exposed


def test_init_failure_does_not_stub_the_instrumentor() -> None:
    """The OTel import and the init() call live in separate try blocks, so an
    init_otel() failure must NOT redefine instrument_fastapi_app as a no-op (the
    regression this split fixes: a transient init error disabling tracing)."""
    body = entrypoint.build_wrapper_body("t", "pkg.mod", "app")
    # The lone no-op stub lives in the *import* except block; everything after
    # the init-failure log line must not define another.
    after_init_except = body.split("'shared_observability init_otel failed'", 1)[1]
    assert "def instrument_fastapi_app" not in after_init_except
    assert body.count("def instrument_fastapi_app") == 1


def test_router_attr_wraps_router_in_fresh_app() -> None:
    body = entrypoint.build_wrapper_body("t", "pkg.mod", "router")
    _compile(body)
    assert "from pkg.mod import router as _router" in body
    assert "app = FastAPI(title='t API')" in body
    assert "app.include_router(_router)" in body


def test_build_wrapper_body_requires_team_identifiers() -> None:
    with pytest.raises(AssertionError):
        entrypoint.build_wrapper_body("", "pkg.mod", "app")
