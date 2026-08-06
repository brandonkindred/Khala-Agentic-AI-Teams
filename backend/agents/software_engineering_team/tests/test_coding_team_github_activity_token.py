"""Unit tests for activity-side GitHub token resolution."""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.fernet import Fernet

from software_engineering_team import token_crypto
from software_engineering_team.tests.conftest import _ensure_real_modules, _stub_orchestrator_only


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Any:
    _ensure_real_modules()
    _stub_orchestrator_only(monkeypatch)
    from software_engineering_team.api import coding_team_main as api_main

    return api_main


def _set_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", Fernet.generate_key().decode())


def _helper():
    from software_engineering_team.temporal.coding_team_github_activities import (
        _require_activity_github_token,
    )

    return _require_activity_github_token


def test_rejects_plaintext_token_key_without_echoing_secret(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    monkeypatch.setattr(api, "get_job", lambda job_id, cache_dir=None: {"job_id": job_id})
    secret = "ghp_should_not_appear"
    with pytest.raises(ValueError, match="token") as exc_info:
        _helper()({"job_id": "job-1", "token": secret})
    assert secret not in str(exc_info.value)


def test_rejects_missing_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(ValueError, match="job_id"):
        _helper()({})


def test_rejects_unknown_job(monkeypatch: pytest.MonkeyPatch, api: Any) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(api, "get_job", lambda job_id, cache_dir=None: None)
    with pytest.raises(ValueError, match="job_id"):
        _helper()({"job_id": "missing"})


def test_resolves_encrypted_job_token(monkeypatch: pytest.MonkeyPatch, api: Any) -> None:
    _set_key(monkeypatch)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    ct = token_crypto.encrypt_token("persisted-pat")
    assert ct is not None
    monkeypatch.setattr(
        api, "get_job", lambda job_id, cache_dir=None: {"github_token_encrypted": ct}
    )
    assert _helper()({"job_id": "job-1"}) == "persisted-pat"


def test_falls_back_to_github_token_env(monkeypatch: pytest.MonkeyPatch, api: Any) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "env-pat")
    monkeypatch.setattr(api, "get_job", lambda job_id, cache_dir=None: {})
    assert _helper()({"job_id": "job-1"}) == "env-pat"


def test_encrypted_prefers_over_env(monkeypatch: pytest.MonkeyPatch, api: Any) -> None:
    _set_key(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "env-pat")
    ct = token_crypto.encrypt_token("persisted-pat")
    assert ct is not None
    monkeypatch.setattr(
        api, "get_job", lambda job_id, cache_dir=None: {"github_token_encrypted": ct}
    )
    assert _helper()({"job_id": "job-1"}) == "persisted-pat"


def test_rejects_when_no_token_available(monkeypatch: pytest.MonkeyPatch, api: Any) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(api, "get_job", lambda job_id, cache_dir=None: {})
    with pytest.raises(ValueError, match="token"):
        _helper()({"job_id": "job-1"})


def test_activity_required_field_tuples_exclude_token() -> None:
    from software_engineering_team.temporal import coding_team_github_activities as mod

    assert "token" not in mod._REQUIRED_FIELDS
    assert "job_id" in mod._REQUIRED_FIELDS
    assert "token" not in mod._PUBLISH_REQUIRED_FIELDS
    assert "job_id" in mod._PUBLISH_REQUIRED_FIELDS
    assert "token" not in mod._FAILURE_NOTICE_REQUIRED_FIELDS
    assert "job_id" in mod._FAILURE_NOTICE_REQUIRED_FIELDS
