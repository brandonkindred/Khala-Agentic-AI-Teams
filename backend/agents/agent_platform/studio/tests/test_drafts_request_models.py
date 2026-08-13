"""Unit tests for drafts HTTP request models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_platform.studio.models import RenameDraftRequest, SaveDraftRequest


def test_save_draft_request_defaults() -> None:
    req = SaveDraftRequest()
    assert req.name is None
    assert req.payload is None


def test_save_draft_request_accepts_name_and_payload() -> None:
    req = SaveDraftRequest(name="Alpha", payload={"teamId": "t1"})
    assert req.name == "Alpha"
    assert req.payload == {"teamId": "t1"}


def test_rename_draft_request_requires_nonempty_name() -> None:
    assert RenameDraftRequest(name="Renamed").name == "Renamed"
    with pytest.raises(ValidationError):
        RenameDraftRequest(name="")
