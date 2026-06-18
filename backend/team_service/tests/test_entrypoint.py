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


def test_env_int_defaults_clamps_and_survives_garbage(monkeypatch) -> None:
    """_env_int parses defensively and clamps to [minimum, maximum]."""
    monkeypatch.delenv("X_WORKERS", raising=False)
    assert entrypoint._env_int("X_WORKERS", 2, minimum=1, maximum=16) == 2  # unset → default
    for bad in ("garbage", "", "   ", "inf", "1e999"):
        monkeypatch.setenv("X_WORKERS", bad)
        assert entrypoint._env_int("X_WORKERS", 2, minimum=1, maximum=16) == 2  # → default
    monkeypatch.setenv("X_WORKERS", "0")
    assert entrypoint._env_int("X_WORKERS", 2, minimum=1, maximum=16) == 1  # floored
    monkeypatch.setenv("X_WORKERS", "1000")
    assert entrypoint._env_int("X_WORKERS", 2, minimum=1, maximum=16) == 16  # ceiling
    monkeypatch.setenv("X_WORKERS", "4")
    assert entrypoint._env_int("X_WORKERS", 2, minimum=1, maximum=16) == 4  # in range
    monkeypatch.setenv("X_WORKERS", "99")
    assert entrypoint._env_int("X_WORKERS", 2, minimum=1) == 99  # no maximum → unbounded


def test_env_int_clamps_out_of_range_default(monkeypatch) -> None:
    """The default fallback is clamped too, so the [minimum, maximum] postcondition
    holds even when a caller passes an out-of-range default (var unset)."""
    monkeypatch.delenv("X_WORKERS", raising=False)
    assert entrypoint._env_int("X_WORKERS", 0, minimum=1, maximum=16) == 1  # below min
    assert entrypoint._env_int("X_WORKERS", 99, minimum=1, maximum=16) == 16  # above max


def test_env_int_warns_on_invalid_value(monkeypatch, caplog) -> None:
    """A set-but-unparseable value falls back to the default AND logs a warning so
    the misconfiguration isn't silent."""
    import logging

    monkeypatch.setenv("X_WORKERS", "abc")
    with caplog.at_level(logging.WARNING, logger=entrypoint.logger.name):
        assert entrypoint._env_int("X_WORKERS", 2, minimum=1, maximum=16) == 2
    assert any("Invalid value for X_WORKERS" in r.getMessage() for r in caplog.records)


def test_env_int_warns_on_fractional_truncation(monkeypatch, caplog) -> None:
    """A fractional value (e.g. '2.5') is truncated AND warned, so the silent
    truncation can't mask a misconfiguration; an integer-valued '2.0' is quiet."""
    import logging

    monkeypatch.setenv("X_WORKERS", "2.5")
    with caplog.at_level(logging.WARNING, logger=entrypoint.logger.name):
        assert entrypoint._env_int("X_WORKERS", 1, minimum=1, maximum=16) == 2
    assert any("Fractional value for X_WORKERS" in r.getMessage() for r in caplog.records)

    caplog.clear()
    monkeypatch.setenv("X_WORKERS", "2.0")  # integer-valued float → no warning
    with caplog.at_level(logging.WARNING, logger=entrypoint.logger.name):
        assert entrypoint._env_int("X_WORKERS", 1, minimum=1, maximum=16) == 2
    assert not any("Fractional value" in r.getMessage() for r in caplog.records)


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
    # Structural check (no reliance on exact log strings): the lone no-op stub
    # lives in the *import* except block, which precedes the init_otel() call.
    assert body.count("def instrument_fastapi_app") == 1
    assert body.index("def instrument_fastapi_app") < body.index("init_otel(service_name=")


def test_router_attr_wraps_router_in_fresh_app() -> None:
    body = entrypoint.build_wrapper_body("t", "pkg.mod", "router")
    _compile(body)
    assert "from pkg.mod import router as _router" in body
    assert "app = FastAPI(title='t API')" in body
    assert "app.include_router(_router)" in body


def test_build_wrapper_body_requires_team_identifiers() -> None:
    with pytest.raises(ValueError):
        entrypoint.build_wrapper_body("", "pkg.mod", "app")


def test_team_name_with_quote_is_embedded_safely() -> None:
    """A team_name containing a quote must not break or inject into the generated
    code — it is embedded via repr(), so the wrapper still compiles."""
    body = entrypoint.build_wrapper_body("ev'il", "pkg.mod", "app")
    _compile(body)  # would SyntaxError if the quote escaped the string literal
    assert 'start_memory_watchdog("ev\'il")' in body or "start_memory_watchdog('ev\\'il')" in body


@pytest.mark.parametrize(
    "team_module,app_attr",
    [
        ("pkg.mod; import os", "app"),  # injection via module path
        ("pkg.mod", "app as x\nimport os"),  # injection via attr
        ("pkg.mod", "1bad"),  # not an identifier
    ],
)
def test_unsafe_module_or_attr_rejected(team_module: str, app_attr: str) -> None:
    with pytest.raises(ValueError):
        entrypoint.build_wrapper_body("t", team_module, app_attr)


def test_wrapper_starts_temporal_worker_when_configured() -> None:
    """With a worker module/func, the wrapper starts the worker in-process,
    gated on TEMPORAL_ADDRESS and resolved via importlib at runtime."""
    body = entrypoint.build_wrapper_body(
        "planning_v3_team",
        "planning_v3_team.api.main",
        "app",
        "planning_v3_team.temporal.worker",
        "start_planning_v3_temporal_worker_thread",
    )
    _compile(body)
    assert "_os.environ.get('TEMPORAL_ADDRESS', '').strip()" in body
    assert "_il.import_module('planning_v3_team.temporal.worker')" in body
    assert "'start_planning_v3_temporal_worker_thread'" in body


def test_wrapper_omits_temporal_worker_when_not_configured() -> None:
    """No worker env vars (the default) → no Temporal block in the wrapper."""
    body = entrypoint.build_wrapper_body("coding_team", "coding_team.api.main", "app")
    _compile(body)
    assert "TEMPORAL_ADDRESS" not in body
    # Partial config (only one of the two) must also emit nothing.
    body_partial = entrypoint.build_wrapper_body(
        "coding_team", "coding_team.api.main", "app", "coding_team.temporal.worker", ""
    )
    assert "TEMPORAL_ADDRESS" not in body_partial


def test_temporal_names_embedded_safely() -> None:
    """Worker module/func names are embedded via repr(), so a hostile value
    cannot inject code — the wrapper still compiles and the payload survives
    only as an inert string literal."""
    payload = "evil'); import os; os.system('x"  # injection attempt
    body = entrypoint.build_wrapper_body("t", "pkg.mod", "app", "pkg.tw", payload)
    _compile(body)  # would SyntaxError if the payload escaped the string literal
    assert repr(payload) in body  # embedded verbatim via repr(), not executable
