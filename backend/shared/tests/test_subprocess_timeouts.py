"""Unit tests for the shared devops subprocess timeout constants (DB-free)."""

from __future__ import annotations

import importlib

import pytest

import shared.subprocess_timeouts as subprocess_timeouts

_CASES = [
    ("DEVOPS_HELM_DRY_RUN_TIMEOUT_S", 120),
    ("DEVOPS_TERRAFORM_EXECUTION_TIMEOUT_S", 180),
    ("DEVOPS_HELM_EXECUTION_TIMEOUT_S", 120),
    ("DEVOPS_CDK_EXECUTION_TIMEOUT_S", 180),
    ("DEVOPS_IAC_VALIDATION_TIMEOUT_S", 120),
    ("DEVOPS_DOCKER_COMPOSE_TIMEOUT_S", 120),
    ("DEVOPS_POLICY_AS_CODE_TIMEOUT_S", 180),
    ("DEVOPS_ARCHITECT_INTEGRATION_TIMEOUT_S", 3600),
]


@pytest.fixture(autouse=True)
def _reload_module_after_test():
    yield
    importlib.reload(subprocess_timeouts)


@pytest.mark.parametrize("name,default", _CASES)
def test_default_when_unset(monkeypatch, name, default) -> None:
    monkeypatch.delenv(name, raising=False)
    importlib.reload(subprocess_timeouts)
    assert getattr(subprocess_timeouts, name) == default


@pytest.mark.parametrize("name,default", _CASES)
def test_env_override(monkeypatch, name, default) -> None:
    monkeypatch.setenv(name, str(default + 5))
    importlib.reload(subprocess_timeouts)
    assert getattr(subprocess_timeouts, name) == default + 5


@pytest.mark.parametrize("name,default", _CASES)
def test_garbage_env_falls_back_to_default(monkeypatch, name, default) -> None:
    monkeypatch.setenv(name, "not-an-int")
    importlib.reload(subprocess_timeouts)
    assert getattr(subprocess_timeouts, name) == default
