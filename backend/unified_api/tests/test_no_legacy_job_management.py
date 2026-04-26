"""Guardrail: no agent code may import the deleted ``shared_job_management``
module or instantiate ``CentralJobManager``. All teams must use
``JobServiceClient`` from ``job_service_client``.

Whitelisted paths: this test file itself, and the historical migration plan
under ``backend/agents/plans/`` which records the original design.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_AGENTS_ROOT = _BACKEND_ROOT / "agents"

_BANNED_PATTERN = re.compile(r"shared_job_management|CentralJobManager")
_WHITELIST_DIRS = {
    _AGENTS_ROOT / "plans",  # historical KIN-5 plan doc
}
_WHITELIST_FILES = {
    Path(__file__).resolve(),  # this guardrail file itself
}


def _is_whitelisted(path: Path) -> bool:
    if path in _WHITELIST_FILES:
        return True
    return any(parent in _WHITELIST_DIRS for parent in path.parents)


def test_no_legacy_job_management_imports() -> None:
    offenders: list[str] = []
    for path in _AGENTS_ROOT.rglob("*.py"):
        if _is_whitelisted(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _BANNED_PATTERN.search(text):
            offenders.append(str(path.relative_to(_BACKEND_ROOT)))
    if offenders:
        pytest.fail(
            "These files import the deleted shared_job_management module "
            "or reference CentralJobManager. Use JobServiceClient from "
            "job_service_client instead:\n  - " + "\n  - ".join(sorted(offenders))
        )
