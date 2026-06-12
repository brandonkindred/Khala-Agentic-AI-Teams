"""Tests for ``product_requirements_analysis_agent.auto_answer``.

Covers the format/parse helpers and the public auto-answer wrappers with the
Strands ``Agent`` mocked out to return canned JSON.
"""

from __future__ import annotations

import json


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

    class _FakeAgent:
        def __init__(self, **kwargs):
            pass

        def __call__(self, prompt):
            return json.dumps(
                {
                    "selected_option_id": "opt1",
                    "rationale": "It's the best",
                    "confidence": 0.9,
                    "risks": [],
                }
            )

    monkeypatch.setattr(auto_answer, "Agent", _FakeAgent)
    monkeypatch.setattr(auto_answer, "get_strands_model", lambda key: None)
    q = _question()
    result = auto_answer.auto_answer_question(
        llm=None, question=q, spec_content="some spec", additional_context="extra"
    )
    assert result.selected_option_id == "opt1"
    assert result.confidence == 0.9


def test_auto_answer_question_llm_exception_uses_default(monkeypatch) -> None:
    from software_engineering_team.product_requirements_analysis_agent import auto_answer

    class _FakeAgent:
        def __init__(self, **kwargs):
            pass

        def __call__(self, prompt):
            raise RuntimeError("LLM down")

    monkeypatch.setattr(auto_answer, "Agent", _FakeAgent)
    monkeypatch.setattr(auto_answer, "get_strands_model", lambda key: None)
    q = _question()
    result = auto_answer.auto_answer_question(llm=None, question=q, spec_content="")
    assert result.selected_option_id == "opt1"  # fell back to default
    assert "Auto-answer failed" in result.rationale


def test_auto_answer_question_no_options_unknown(monkeypatch) -> None:
    from software_engineering_team.product_requirements_analysis_agent import auto_answer

    class _FakeAgent:
        def __init__(self, **kwargs):
            pass

        def __call__(self, prompt):
            raise RuntimeError("boom")

    monkeypatch.setattr(auto_answer, "Agent", _FakeAgent)
    monkeypatch.setattr(auto_answer, "get_strands_model", lambda key: None)
    q = _question(options=[])
    result = auto_answer.auto_answer_question(llm=None, question=q, spec_content="")
    assert result.selected_option_id == "unknown"


def test_auto_answer_all_questions(monkeypatch) -> None:
    from software_engineering_team.product_requirements_analysis_agent import auto_answer

    class _FakeAgent:
        def __init__(self, **kwargs):
            pass

        def __call__(self, prompt):
            return json.dumps(
                {"selected_option_id": "opt1", "rationale": "r", "confidence": 0.7}
            )

    monkeypatch.setattr(auto_answer, "Agent", _FakeAgent)
    monkeypatch.setattr(auto_answer, "get_strands_model", lambda key: None)
    questions = [_question(id="q1"), _question(id="q2")]
    results = auto_answer.auto_answer_all_questions(
        llm=None, questions=questions, spec_content=""
    )
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

    class _FakeAgent:
        def __init__(self, **kwargs):
            pass

        def __call__(self, prompt):
            return json.dumps(
                {"selected_option_id": "o1", "rationale": "r", "confidence": 0.7}
            )

    monkeypatch.setattr(auto_answer, "Agent", _FakeAgent)
    monkeypatch.setattr(auto_answer, "get_strands_model", lambda key: None)
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
