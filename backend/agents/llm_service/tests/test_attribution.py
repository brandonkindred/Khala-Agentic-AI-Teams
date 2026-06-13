"""Tests for the per-call LLM attribution context (attribution.py)."""

import pytest

from llm_service.attribution import (
    LLMAttribution,
    bind_request_id,
    caller_agent,
    current_attribution,
    current_request_id,
    llm_attribution,
    new_request_id,
)


def _agent_from(path: str) -> str:
    """Invoke caller_agent as if from a frame whose source file is ``path``."""

    def probe() -> str:
        return caller_agent()

    ns: dict = {}
    exec(compile("def f(probe):\n    return probe()\n", path, "exec"), ns)
    return ns["f"](probe)


def test_caller_agent_uses_package_dir_or_file_stem() -> None:
    # The agent's own package directory is the identity...
    assert (
        _agent_from(
            "/x/agents/software_engineering_team/frontend_code_v2_team/tool_agents/ui_design/agent.py"
        )
        == "ui_design"
    )
    # ...but when the file sits in a generic container, the file stem is used.
    assert _agent_from("/x/agents/job_matching_team/agents/ranker.py") == "ranker"
    assert (
        _agent_from("/x/agents/software_engineering_team/agent_implementations/run_team.py")
        == "run_team"
    )


def test_caller_agent_skips_llm_service_and_non_team_frames() -> None:
    assert _agent_from("/x/agents/llm_service/factory.py") == ""
    assert _agent_from("/some/site-packages/strands/agent.py") == ""


def test_default_is_empty() -> None:
    attr = current_attribution()
    assert attr == LLMAttribution()
    assert attr.agent_key == "" and attr.team == "" and attr.objective == "" and attr.job_id == ""
    assert current_request_id() == ""


def test_set_and_restore() -> None:
    with llm_attribution(team="blogging", objective="draft intro"):
        attr = current_attribution()
        assert attr.team == "blogging"
        assert attr.objective == "draft intro"
        assert attr.agent_key == ""
    # Restored on exit.
    assert current_attribution() == LLMAttribution()


def test_nested_inherits_and_overrides() -> None:
    with llm_attribution(team="t", objective="outer", job_id="J1"):
        with llm_attribution(agent_key="ag", objective="inner"):
            inner = current_attribution()
            # agent_key + objective overridden; team + job_id inherited.
            assert inner.agent_key == "ag"
            assert inner.objective == "inner"
            assert inner.team == "t"
            assert inner.job_id == "J1"
        # Inner block fully restored.
        outer = current_attribution()
        assert outer.agent_key == ""
        assert outer.objective == "outer"
        assert outer.team == "t"


def test_empty_string_override_is_distinct_from_none_inherit() -> None:
    with llm_attribution(objective="outer"):
        with llm_attribution(objective=""):
            assert current_attribution().objective == ""
        assert current_attribution().objective == "outer"


def test_restore_after_exception() -> None:
    with pytest.raises(RuntimeError):
        with llm_attribution(team="t", objective="o"):
            raise RuntimeError("boom")
    assert current_attribution() == LLMAttribution()


def test_bind_request_id_set_and_restore() -> None:
    assert current_request_id() == ""
    with bind_request_id("rid-1"):
        assert current_request_id() == "rid-1"
        with bind_request_id("rid-2"):
            assert current_request_id() == "rid-2"
        assert current_request_id() == "rid-1"
    assert current_request_id() == ""


def test_bind_request_id_restores_after_exception() -> None:
    with pytest.raises(ValueError):
        with bind_request_id("rid-x"):
            raise ValueError("nope")
    assert current_request_id() == ""


def test_bind_request_id_rejects_empty() -> None:
    # ValueError (not AssertionError): the precondition must hold under ``python -O``.
    with pytest.raises(ValueError, match="request_id must be a non-empty string"):
        with bind_request_id(""):
            pass


def test_new_request_id_is_unique_and_short() -> None:
    a = new_request_id()
    b = new_request_id()
    assert a != b
    assert len(a) == 12 and len(b) == 12
    assert all(ch in "0123456789abcdef" for ch in a)
