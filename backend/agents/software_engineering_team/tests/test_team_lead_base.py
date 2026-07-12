"""Tests for the shared BaseTeamLead / copy_development_result_fields."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from software_engineering_team.shared.team_lead_base import (
    BaseTeamLead,
    copy_development_result_fields,
)


def _make_lead(**overrides) -> BaseTeamLead:
    kwargs = dict(
        extensions=frozenset({".py"}),
        exclude_dirs=frozenset({".git"}),
        max_chars=1000,
    )
    kwargs.update(overrides)
    return BaseTeamLead(MagicMock(), **kwargs)


def test_init_requires_llm_client():
    with pytest.raises(AssertionError):
        BaseTeamLead(None, extensions=frozenset(), exclude_dirs=frozenset(), max_chars=100)


def test_init_stores_llm_and_starts_with_empty_cache_dict():
    llm = MagicMock()
    lead = BaseTeamLead(llm, extensions=frozenset({".py"}), exclude_dirs=frozenset(), max_chars=100)
    assert lead.llm is llm
    assert lead._repo_context_caches == {}


def test_repo_context_cache_for_is_lazy_and_reused(tmp_path: Path):
    lead = _make_lead()
    first = lead._repo_context_cache_for(tmp_path)
    second = lead._repo_context_cache_for(tmp_path)
    assert first is second  # same resolved repo -> reused, not rebuilt

    other = tmp_path / "other"
    other.mkdir()
    third = lead._repo_context_cache_for(other)
    assert third is not first  # distinct repo -> distinct cache


def test_repo_context_cache_for_uses_injected_constants(tmp_path: Path):
    extensions = frozenset({".ts", ".tsx"})
    exclude_dirs = frozenset({"dist"})
    lead = _make_lead(extensions=extensions, exclude_dirs=exclude_dirs, max_chars=42)
    cache = lead._repo_context_cache_for(tmp_path)
    assert cache._ext_set == extensions
    assert cache._excl_set == exclude_dirs
    assert cache._max_chars == 42


def test_repo_context_cache_for_rejects_non_directory(tmp_path: Path):
    lead = _make_lead()
    not_a_dir = tmp_path / "missing"
    with pytest.raises(AssertionError):
        lead._repo_context_cache_for(not_a_dir)


def test_copy_development_result_fields_copies_all_shared_fields():
    src = SimpleNamespace(
        success=True,
        current_phase="deliver",
        iterations_used=3,
        planning_result="planning",
        execution_result="execution",
        review_result="review",
        problem_solving_result="problem_solving",
        documentation_result="documentation",
        deliver_result="deliver",
        final_files={"a.py": "x = 1"},
        summary="done",
        failure_reason="",
        needs_followup=True,
        setup_result="src-setup",  # must NOT be copied
    )
    dst = SimpleNamespace(
        success=False,
        current_phase="setup",
        iterations_used=0,
        planning_result=None,
        execution_result=None,
        review_result=None,
        problem_solving_result=None,
        documentation_result=None,
        deliver_result=None,
        final_files={},
        summary="",
        failure_reason="",
        needs_followup=False,
        setup_result="dst-setup",  # must survive untouched
    )

    copy_development_result_fields(dst, src)

    assert dst.success is True
    assert dst.current_phase == "deliver"
    assert dst.iterations_used == 3
    assert dst.planning_result == "planning"
    assert dst.execution_result == "execution"
    assert dst.review_result == "review"
    assert dst.problem_solving_result == "problem_solving"
    assert dst.documentation_result == "documentation"
    assert dst.deliver_result == "deliver"
    assert dst.final_files == {"a.py": "x = 1"}
    assert dst.summary == "done"
    assert dst.needs_followup is True
    # setup_result is deliberately excluded from the copy.
    assert dst.setup_result == "dst-setup"
