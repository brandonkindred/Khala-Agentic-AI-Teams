"""Tests for ``product_requirements_analysis_agent.auto_answer``.

Covers the format/parse helpers and the public auto-answer wrappers with
``complete_json_with_continuation`` mocked out to return canned JSON (one test
instead recovers a markdown-fenced response end-to-end through the real
shared-helper parsing logic).
"""

from __future__ import annotations


def _question(**overrides):
    from software_engineering_team.product_requirements_analysis_agent.models import (
        OpenQuestion,
        QuestionOption,
    )

    base = dict(
        id="q1",
        question_text="Which database?",
        context="for storage",
        options=[
            QuestionOption(id="opt1", label="PostgreSQL", is_default=True, confidence=0.9),
            QuestionOption(id="opt2", label="MySQL", rationale="alt", confidence=0.5),
        ],
        source="spec_review",
    )
    base.update(overrides)
    return OpenQuestion(**base)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_format_options_for_prompt() -> None:
    from software_engineering_team.product_requirements_analysis_agent.auto_answer import (
        _format_options_for_prompt,
    )

    q = _question()
    text = _format_options_for_prompt(q.options)
    assert "ID: opt1" in text
    assert "PostgreSQL" in text
    assert "(Marked as recommended default)" in text
    assert "rationale: alt" in text


def test_get_default_option_prefers_default_flag() -> None:
    from software_engineering_team.product_requirements_analysis_agent.auto_answer import (
        _get_default_option,
    )

    q = _question()
    opt = _get_default_option(q)
    assert opt is not None
    assert opt.id == "opt1"


def test_get_default_option_falls_back_to_highest_confidence() -> None:
    from software_engineering_team.product_requirements_analysis_agent.auto_answer import (
        _get_default_option,
    )
    from software_engineering_team.product_requirements_analysis_agent.models import (
        QuestionOption,
    )

    q = _question(
        options=[
            QuestionOption(id="a", label="A", confidence=0.3),
            QuestionOption(id="b", label="B", confidence=0.9),
        ]
    )
    opt = _get_default_option(q)
    assert opt is not None
    assert opt.id == "b"


def test_get_default_option_empty_options() -> None:
    from software_engineering_team.product_requirements_analysis_agent.auto_answer import (
        _get_default_option,
    )

    q = _question(options=[])
    assert _get_default_option(q) is None


def test_parse_auto_answer_response_happy() -> None:
    from software_engineering_team.product_requirements_analysis_agent.auto_answer import (
        _parse_auto_answer_response,
    )

    q = _question()
    raw = {
        "selected_option_id": "opt1",
        "rationale": "Best for our scale",
        "confidence": 0.9,
        "risks": ["lock-in"],
        "alternatives_considered": "MySQL",
        "industry_references": ["pg.org"],
    }
    out = _parse_auto_answer_response(raw, q)
    assert out.selected_option_id == "opt1"
    assert out.confidence == 0.9
    assert out.risks == ["lock-in"]
    assert out.industry_references == ["pg.org"]


def test_parse_auto_answer_response_non_dict_uses_default() -> None:
    from software_engineering_team.product_requirements_analysis_agent.auto_answer import (
        _parse_auto_answer_response,
    )

    q = _question()
    out = _parse_auto_answer_response("not a dict", q)  # type: ignore[arg-type]
    assert out.selected_option_id == "opt1"
    assert "parsing failed" in out.rationale.lower()


def test_parse_auto_answer_response_unknown_id_falls_back() -> None:
    from software_engineering_team.product_requirements_analysis_agent.auto_answer import (
        _parse_auto_answer_response,
    )

    q = _question()
    raw = {"selected_option_id": "missing", "rationale": "n/a", "confidence": 0.5}
    out = _parse_auto_answer_response(raw, q)
    assert out.selected_option_id == "opt1"  # fell back to default


def test_parse_auto_answer_response_risks_non_list() -> None:
    from software_engineering_team.product_requirements_analysis_agent.auto_answer import (
        _parse_auto_answer_response,
    )

    q = _question()
    raw = {
        "selected_option_id": "opt1",
        "rationale": "r",
        "confidence": 0.7,
        "risks": "not a list",
        "industry_references": "not a list",
    }
    out = _parse_auto_answer_response(raw, q)
    assert out.risks == []
    assert out.industry_references == []


# ---------------------------------------------------------------------------
# auto_answer_question / auto_answer_all_questions
# ---------------------------------------------------------------------------


def test_auto_answer_question_happy(monkeypatch) -> None:
    from software_engineering_team.product_requirements_analysis_agent import auto_answer

    def _fake_complete_json(model, prompt, *, system_prompt=None, **kwargs):
        return {
            "selected_option_id": "opt1",
            "rationale": "It's the best",
            "confidence": 0.9,
            "risks": [],
        }

    monkeypatch.setattr(auto_answer, "complete_json_with_continuation", _fake_complete_json)
    monkeypatch.setattr(auto_answer, "get_strands_model", lambda key, **kw: None)
    q = _question()
    result = auto_answer.auto_answer_question(
        llm=None, question=q, spec_content="some spec", additional_context="extra"
    )
    assert result.selected_option_id == "opt1"
    assert result.confidence == 0.9


def test_auto_answer_question_llm_exception_uses_default(monkeypatch) -> None:
    from software_engineering_team.product_requirements_analysis_agent import auto_answer

    def _raise_complete_json(model, prompt, *, system_prompt=None, **kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(auto_answer, "complete_json_with_continuation", _raise_complete_json)
    monkeypatch.setattr(auto_answer, "get_strands_model", lambda key, **kw: None)
    q = _question()
    result = auto_answer.auto_answer_question(llm=None, question=q, spec_content="")
    assert result.selected_option_id == "opt1"  # fell back to default
    assert "Auto-answer failed" in result.rationale


def test_auto_answer_question_no_options_unknown(monkeypatch) -> None:
    from software_engineering_team.product_requirements_analysis_agent import auto_answer

    def _raise_complete_json(model, prompt, *, system_prompt=None, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(auto_answer, "complete_json_with_continuation", _raise_complete_json)
    monkeypatch.setattr(auto_answer, "get_strands_model", lambda key, **kw: None)
    q = _question(options=[])
    result = auto_answer.auto_answer_question(llm=None, question=q, spec_content="")
    assert result.selected_option_id == "unknown"


def test_auto_answer_question_uses_provided_llm_client(monkeypatch) -> None:
    """A real LLMClient passed as ``llm`` must actually reach
    get_strands_model(..., client=<that object>) via resolve_strands_model,
    instead of being silently ignored in favor of a hardcoded default model
    (the bug an automated review flagged on this migration's own PR).

    Uses a plain ``LLMClient`` (not ``DummyLLMClient``, which also implements
    the Strands ``Model`` ABC and would short-circuit resolve_strands_model's
    first branch, returning itself without ever reaching get_strands_model)."""
    from llm_service import LLMClient
    from software_engineering_team.product_requirements_analysis_agent import auto_answer
    from software_engineering_team.tests.conftest import _strands_model_double

    class _PlainLLMClient(LLMClient):
        def complete_json(self, prompt, *, objective="", **kwargs):
            raise AssertionError("complete_json_with_continuation is mocked; should not be called")

    calls = []

    def _spy_get_strands_model(agent_key, **kwargs):
        calls.append((agent_key, kwargs))
        return _strands_model_double()

    def _fake_complete_json(model, prompt, *, system_prompt=None, **kwargs):
        return {"selected_option_id": "opt1", "rationale": "used my client", "confidence": 0.9}

    my_llm = _PlainLLMClient()
    monkeypatch.setattr(auto_answer, "complete_json_with_continuation", _fake_complete_json)
    monkeypatch.setattr(auto_answer, "get_strands_model", _spy_get_strands_model)
    q = _question()
    result = auto_answer.auto_answer_question(llm=my_llm, question=q, spec_content="some spec")

    assert result.selected_option_id == "opt1"
    assert len(calls) == 1
    agent_key, kwargs = calls[0]
    assert agent_key == "product_analysis"
    assert kwargs.get("client") is my_llm


def test_auto_answer_question_recovers_fenced_json_response(monkeypatch) -> None:
    """End-to-end (no complete_json_with_continuation mocking): a markdown-fenced
    LLM response is recovered instead of crashing on a bare json.loads, exercising
    the real extract_json_from_response fallback through the shared helper."""
    from software_engineering_team.product_requirements_analysis_agent import auto_answer
    from software_engineering_team.tests.conftest import (
        _patch_fenced_response,
        _strands_model_double,
    )

    payload = {
        "selected_option_id": "opt1",
        "rationale": "fenced pick",
        "confidence": 0.8,
        "risks": [],
    }
    _patch_fenced_response(monkeypatch, payload)
    monkeypatch.setattr(auto_answer, "get_strands_model", lambda key, **kw: _strands_model_double())
    q = _question()
    result = auto_answer.auto_answer_question(llm=None, question=q, spec_content="some spec")
    assert result.selected_option_id == "opt1"
    assert result.rationale == "fenced pick"


def test_auto_answer_all_questions(monkeypatch) -> None:
    from software_engineering_team.product_requirements_analysis_agent import auto_answer

    def _fake_complete_json(model, prompt, *, system_prompt=None, **kwargs):
        return {"selected_option_id": "opt1", "rationale": "r", "confidence": 0.7}

    monkeypatch.setattr(auto_answer, "complete_json_with_continuation", _fake_complete_json)
    monkeypatch.setattr(auto_answer, "get_strands_model", lambda key, **kw: None)
    questions = [_question(id="q1"), _question(id="q2")]
    results = auto_answer.auto_answer_all_questions(llm=None, questions=questions, spec_content="")
    assert len(results) == 2


# ---------------------------------------------------------------------------
# get_auto_answer_for_job
# ---------------------------------------------------------------------------


def test_get_auto_answer_for_job_not_found(monkeypatch, patched_job_store) -> None:
    from software_engineering_team.product_requirements_analysis_agent import auto_answer

    out = auto_answer.get_auto_answer_for_job(
        llm=None, job_id="ghost", question_id="q1", spec_content=""
    )
    assert out is None


def test_get_auto_answer_for_job_no_options_returns_none(patched_job_store) -> None:
    from software_engineering_team.product_requirements_analysis_agent import auto_answer
    from software_engineering_team.shared import job_store as js

    js.create_job("job1", repo_path="/tmp")
    js.update_job(
        "job1",
        pending_questions=[
            {"id": "q1", "question_text": "What fields?", "context": "", "options": []}
        ],
    )
    # Questions with no options cannot be auto-answered; the user must provide free-text.
    out = auto_answer.get_auto_answer_for_job(
        llm=None, job_id="job1", question_id="q1", spec_content=""
    )
    assert out is None


def test_get_auto_answer_for_job_synthetic_other_option_returns_none(patched_job_store) -> None:
    from software_engineering_team.product_requirements_analysis_agent import auto_answer
    from software_engineering_team.shared import job_store as js

    js.create_job("job2", repo_path="/tmp")
    js.update_job(
        "job2",
        pending_questions=[
            {
                "id": "q1",
                "question_text": "What is the deployment target?",
                "context": "",
                # Synthetic placeholder inserted by _convert_to_pending_questions when the
                # OpenQuestion had no options — must NOT be treated as a real selectable option.
                "options": [{"id": "other", "label": "Provide answer in text field"}],
            }
        ],
    )
    out = auto_answer.get_auto_answer_for_job(
        llm=None, job_id="job2", question_id="q1", spec_content=""
    )
    assert out is None


def test_get_auto_answer_for_job_question_not_found(patched_job_store) -> None:
    from software_engineering_team.product_requirements_analysis_agent import auto_answer
    from software_engineering_team.shared import job_store as js

    js.create_job("job1", repo_path="/tmp")
    out = auto_answer.get_auto_answer_for_job(
        llm=None, job_id="job1", question_id="ghost", spec_content=""
    )
    assert out is None


def test_get_auto_answer_for_job_happy(monkeypatch, patched_job_store) -> None:
    from software_engineering_team.product_requirements_analysis_agent import auto_answer
    from software_engineering_team.shared import job_store as js

    def _fake_complete_json(model, prompt, *, system_prompt=None, **kwargs):
        return {"selected_option_id": "o1", "rationale": "r", "confidence": 0.7}

    monkeypatch.setattr(auto_answer, "complete_json_with_continuation", _fake_complete_json)
    monkeypatch.setattr(auto_answer, "get_strands_model", lambda key, **kw: None)
    js.create_job("job1", repo_path="/tmp")
    js.update_job(
        "job1",
        pending_questions=[
            {
                "id": "q1",
                "question_text": "Q?",
                "context": "ctx",
                "options": [{"id": "o1", "label": "L1", "is_default": True}],
                "source": "spec",
                "category": "general",
                "priority": "medium",
            }
        ],
    )
    out = auto_answer.get_auto_answer_for_job(
        llm=None, job_id="job1", question_id="q1", spec_content=""
    )
    assert out is not None
    assert out.selected_option_id == "o1"
