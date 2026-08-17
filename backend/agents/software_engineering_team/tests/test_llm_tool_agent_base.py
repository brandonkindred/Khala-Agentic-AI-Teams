"""Unit tests for the dependency-light ``LlmToolAgentBase`` skeleton."""

from __future__ import annotations

import os
import subprocess
import sys

from software_engineering_team.shared.llm_tool_agent_base import FallbackPayload, LlmToolAgentBase

# Mirrors pytest.ini's `pythonpath = agents .` so the subprocess below can
# import `software_engineering_team` the same way the test runner does.
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
_AGENTS_ROOT = os.path.join(_BACKEND_ROOT, "agents")

# ---------------------------------------------------------------------------
# constructor
# ---------------------------------------------------------------------------


def test_constructor_stores_llm():
    sentinel = object()
    agent = LlmToolAgentBase(llm=sentinel)
    assert agent.llm is sentinel


def test_constructor_defaults_llm_to_none():
    agent = LlmToolAgentBase()
    assert agent.llm is None


# ---------------------------------------------------------------------------
# _agent_factory (monkeypatch resolution)
# ---------------------------------------------------------------------------


class _DemoAgent(LlmToolAgentBase):
    pass


# Provide a module-level Agent symbol so _agent_factory (which resolves Agent
# from the subclass's defining module) can find and patch it.
Agent = None


def test_agent_factory_resolves_from_defining_module(monkeypatch):
    sentinel_factory = object()
    monkeypatch.setattr(
        sys.modules[_DemoAgent.__module__], "Agent", sentinel_factory, raising=False
    )

    agent = _DemoAgent()

    assert agent._agent_factory() is sentinel_factory


# ---------------------------------------------------------------------------
# dependency purity: importing this module must never pull in
# code_review_agent (a regression test, not just an assertion in prose)
# ---------------------------------------------------------------------------


def test_module_import_does_not_pull_in_code_review_agent():
    # Run in a fresh subprocess: other tests in this session may have already
    # imported code_review_agent, which would make an in-process sys.modules
    # check order-dependent and unreliable.
    script = (
        "import sys\n"
        "import software_engineering_team.shared.llm_tool_agent_base\n"
        "polluted = [n for n in sys.modules if n.endswith('code_review_agent') "
        "or '.code_review_agent.' in n]\n"
        "assert not polluted, polluted\n"
    )
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([_AGENTS_ROOT, _BACKEND_ROOT]))
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# opt-in model resolution (parameterized recipes)
# ---------------------------------------------------------------------------


def _patch_resolve(monkeypatch, fake):
    """Patch the lazy-imported resolver before constructing an opted-in agent."""
    monkeypatch.setattr("llm_service.strands_model.resolve_strands_model", fake)


def test_resolve_models_false_does_not_set_model(monkeypatch):
    calls = []

    def fake_resolve(llm, **kwargs):
        calls.append((llm, kwargs))
        return object()

    _patch_resolve(monkeypatch, fake_resolve)

    agent = LlmToolAgentBase(llm=object())

    assert not hasattr(agent, "_model")
    assert not hasattr(agent, "_model_json")
    assert calls == []


def test_review_like_resolves_text_model_without_get_strands_model_fn(monkeypatch):
    calls = []
    text_model = object()

    def fake_resolve(llm, **kwargs):
        calls.append((llm, dict(kwargs)))
        return text_model

    _patch_resolve(monkeypatch, fake_resolve)

    class ReviewLike(LlmToolAgentBase):
        resolve_models = True

    llm = object()
    agent = ReviewLike(llm=llm)

    assert agent._model is text_model
    assert not hasattr(agent, "_model_json")
    assert len(calls) == 1
    assert calls[0][0] is llm
    assert calls[0][1] == {"response_format": "text"}
    assert "get_strands_model_fn" not in calls[0][1]


def test_review_like_uses_json_model_resolves_second_json_model(monkeypatch):
    calls = []
    text_model = object()
    json_model = object()

    def fake_resolve(llm, **kwargs):
        calls.append((llm, dict(kwargs)))
        return text_model if kwargs.get("response_format") == "text" else json_model

    _patch_resolve(monkeypatch, fake_resolve)

    class ReviewJsonLike(LlmToolAgentBase):
        resolve_models = True
        uses_json_model = True

    llm = object()
    agent = ReviewJsonLike(llm=llm)

    assert agent._model is text_model
    assert agent._model_json is json_model
    assert len(calls) == 2
    assert calls[0][1] == {"response_format": "text"}
    assert calls[1][1] == {"response_format": "json"}
    assert "get_strands_model_fn" not in calls[0][1]
    assert "get_strands_model_fn" not in calls[1][1]


def test_get_strands_model_fn_class_attr_forwards_unbound_function(monkeypatch):
    """Class-level function attrs must not be bound via self access."""
    calls = []

    def fake_get_strands_model(llm, *, response_format):
        return object()

    def fake_resolve(llm, **kwargs):
        calls.append((llm, dict(kwargs)))
        return object()

    _patch_resolve(monkeypatch, fake_resolve)

    class PlanJsonLike(LlmToolAgentBase):
        resolve_models = True
        response_format = "json"
        get_strands_model_fn = fake_get_strands_model

    llm = object()
    PlanJsonLike(llm=llm)

    assert len(calls) == 1
    forwarded = calls[0][1]["get_strands_model_fn"]
    assert forwarded is fake_get_strands_model
    assert not hasattr(forwarded, "__self__")


def test_plan_json_like_resolves_with_get_strands_model_fn(monkeypatch):
    calls = []
    json_model = object()
    sentinel_fn = object()

    def fake_resolve(llm, **kwargs):
        calls.append((llm, dict(kwargs)))
        return json_model

    _patch_resolve(monkeypatch, fake_resolve)

    class PlanJsonLike(LlmToolAgentBase):
        resolve_models = True
        response_format = "json"
        get_strands_model_fn = sentinel_fn

    llm = object()
    agent = PlanJsonLike(llm=llm)

    assert agent._model is json_model
    assert not hasattr(agent, "_model_json")
    assert len(calls) == 1
    assert calls[0][0] is llm
    assert calls[0][1] == {
        "response_format": "json",
        "get_strands_model_fn": sentinel_fn,
    }


# ---------------------------------------------------------------------------
# opt-in LLM invocation (inline vs run_strands_agent wrapper)
# ---------------------------------------------------------------------------


def test_invoke_llm_inline_path_strips_agent_output(monkeypatch):
    """Default path: str(agent_factory()(model=model)(prompt)).strip()."""
    calls = []

    class _FakeAgent:
        def __init__(self, *, model):
            calls.append(("construct", model))

        def __call__(self, prompt):
            calls.append(("call", prompt))
            return "  hello world  \n"

    class InlineLike(LlmToolAgentBase):
        pass

    agent = InlineLike()
    monkeypatch.setattr(
        type(agent),
        "_agent_factory",
        lambda self: _FakeAgent,
    )

    model = object()
    result = agent._invoke_llm(model, "do the thing")

    assert result == "hello world"
    assert calls == [("construct", model), ("call", "do the thing")]


def test_invoke_llm_inline_path_does_not_call_run_strands_agent(monkeypatch):
    """Inline path must not touch run_strands_agent."""
    wrapper_calls = []

    def fake_run_strands_agent(agent_factory, model, prompt):
        wrapper_calls.append((agent_factory, model, prompt))
        return "from-wrapper"

    monkeypatch.setattr("llm_service.strands_model.run_strands_agent", fake_run_strands_agent)

    class _FakeAgent:
        def __init__(self, *, model):
            pass

        def __call__(self, prompt):
            return "from-inline"

    class InlineLike(LlmToolAgentBase):
        pass

    agent = InlineLike()
    monkeypatch.setattr(type(agent), "_agent_factory", lambda self: _FakeAgent)

    result = agent._invoke_llm(object(), "prompt")

    assert result == "from-inline"
    assert wrapper_calls == []


def test_invoke_llm_wrapped_path_delegates_to_run_strands_agent(monkeypatch):
    """When use_run_strands_agent is True, delegate to run_strands_agent."""
    wrapper_calls = []
    sentinel_factory = object()

    def fake_run_strands_agent(agent_factory, model, prompt):
        wrapper_calls.append((agent_factory, model, prompt))
        return "wrapped-result"

    monkeypatch.setattr("llm_service.strands_model.run_strands_agent", fake_run_strands_agent)

    class WrappedLike(LlmToolAgentBase):
        use_run_strands_agent = True

    agent = WrappedLike()
    monkeypatch.setattr(type(agent), "_agent_factory", lambda self: sentinel_factory)

    model = object()
    result = agent._invoke_llm(model, "the prompt")

    assert result == "wrapped-result"
    assert len(wrapper_calls) == 1
    assert wrapper_calls[0][0] is sentinel_factory
    assert wrapper_calls[0][1] is model
    assert wrapper_calls[0][2] == "the prompt"


# ---------------------------------------------------------------------------
# parameterized JSON parsing (lenient vs extract; text mode)
# ---------------------------------------------------------------------------


def test_lenient_json_success_parses_object():
    class ReviewJsonLike(LlmToolAgentBase):
        json_parse_strategy = "lenient"
        review_parse_mode = "json"
        parse_context = "unit-test"
        parse_on_fail_msg = "reporting empty."

    agent = ReviewJsonLike()
    assert agent._parse_llm_json('{"ok": true, "n": 1}') == {"ok": True, "n": 1}


def test_lenient_json_failure_returns_empty_dict_not_none():
    """Real engine sentinel: unparseable input must yield {} (not None)."""

    class ReviewJsonLike(LlmToolAgentBase):
        json_parse_strategy = "lenient"
        review_parse_mode = "json"
        parse_context = "unit-test"
        parse_on_fail_msg = "reporting empty."

    agent = ReviewJsonLike()
    result = agent._parse_llm_json("no json object here at all")
    assert result == {}
    assert result is not None


def test_lenient_text_mode_calls_parse_review_hook(monkeypatch):
    calls = []
    engine_calls = []

    def fake_parse_review(raw: str):
        calls.append(raw)
        return {"issues": [{"description": "from-hook"}]}

    def boom_lenient(*args, **kwargs):
        engine_calls.append(("lenient", args, kwargs))
        raise AssertionError("lenient_json_object must not be called in text mode")

    def boom_extract(*args, **kwargs):
        engine_calls.append(("extract", args, kwargs))
        raise AssertionError("parse_json_object must not be called in text mode")

    monkeypatch.setattr(
        "software_engineering_team.shared.tool_agent_base.lenient_json_object",
        boom_lenient,
    )
    monkeypatch.setattr(
        "software_engineering_team.shared.json_utils.parse_json_object",
        boom_extract,
        raising=False,
    )

    class ReviewTextLike(LlmToolAgentBase):
        json_parse_strategy = "lenient"
        review_parse_mode = "text"
        _parse_review = staticmethod(fake_parse_review)

    agent = ReviewTextLike()
    result = agent._parse_llm_json("TEMPLATE OUTPUT")

    assert result == {"issues": [{"description": "from-hook"}]}
    assert calls == ["TEMPLATE OUTPUT"]
    assert engine_calls == []


def test_extract_success_returns_dict(monkeypatch):
    def fake_extract(raw: str):
        assert raw == '{"a": 1}'
        return {"a": 1}

    monkeypatch.setattr(
        "software_engineering_team.shared.json_utils.parse_json_object", fake_extract
    )

    class PlanJsonLike(LlmToolAgentBase):
        json_parse_strategy = "extract"

    agent = PlanJsonLike()
    assert agent._parse_llm_json('{"a": 1}') == {"a": 1}


def test_extract_failure_returns_none_not_empty_dict():
    """Real engine sentinel: unparseable input must yield None (not {})."""

    class PlanJsonLike(LlmToolAgentBase):
        json_parse_strategy = "extract"

    agent = PlanJsonLike()
    result = agent._parse_llm_json("no json object here at all")
    assert result is None


# ---------------------------------------------------------------------------
# fallback taxonomy (opt-in helpers; not wired into consumer bases yet)
# ---------------------------------------------------------------------------


class _FallbackAgent(LlmToolAgentBase):
    no_model_recommendations = ["no-model-rec"]
    no_model_summary = "no-model-summary"
    llm_error_recommendations = ["llm-error-rec"]
    llm_error_summary = "llm-error-summary"
    empty_recommendations = ["empty-rec"]
    default_summary = "default-summary"
    empty_summary_override = "empty-override"


def test_fallback_no_model_returns_payload_when_model_falsy():
    agent = _FallbackAgent()
    payload = agent._fallback_no_model(None)

    assert payload == FallbackPayload(
        tier="no_model",
        recommendations=["no-model-rec"],
        summary="no-model-summary",
    )
    # Returned list is a copy of the class attr, not the same object.
    assert payload.recommendations is not _FallbackAgent.no_model_recommendations


def test_fallback_no_model_returns_none_when_model_truthy():
    agent = _FallbackAgent()
    assert agent._fallback_no_model(object()) is None


def test_fallback_no_model_returns_payload_when_model_empty_string():
    agent = _FallbackAgent()
    payload = agent._fallback_no_model("")

    assert payload is not None
    assert payload.tier == "no_model"


def test_call_with_single_fallback_success():
    agent = _FallbackAgent()
    status, value = agent._call_with_single_fallback(lambda: "ok-value", log_label="demo")

    assert status == "ok"
    assert value == "ok-value"


def test_call_with_single_fallback_exception_returns_call_error(caplog):
    import logging

    agent = _FallbackAgent()

    with caplog.at_level(logging.WARNING):
        status, payload = agent._call_with_single_fallback(
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            log_label="demo-call",
        )

    assert status == "error"
    assert payload == FallbackPayload(
        tier="call_error",
        recommendations=["llm-error-rec"],
        summary="llm-error-summary",
    )
    assert payload.recommendations is not _FallbackAgent.llm_error_recommendations
    assert any(
        r.name == _FallbackAgent.__module__ and "demo-call" in r.message and "boom" in r.message
        for r in caplog.records
    )


def test_call_partial_tolerant_keeps_successes_skips_failures(caplog):
    import logging

    agent = _FallbackAgent()

    def flaky(item):
        if item == "bad":
            raise ValueError("nope")
        return f"ok:{item}"

    with caplog.at_level(logging.WARNING):
        results = agent._call_partial_tolerant(["a", "bad", "c"], flaky, log_label="partial")

    assert results == ["ok:a", "ok:c"]
    assert any(
        r.name == _FallbackAgent.__module__ and "partial" in r.message and "nope" in r.message
        for r in caplog.records
    )


def test_call_partial_tolerant_empty_items():
    agent = _FallbackAgent()
    assert agent._call_partial_tolerant([], lambda x: x) == []


def test_call_partial_tolerant_truncates_long_item_context(caplog):
    import logging

    agent = _FallbackAgent()
    long_item = "x" * 80

    with caplog.at_level(logging.WARNING):
        results = agent._call_partial_tolerant(
            [long_item],
            lambda _item: (_ for _ in ()).throw(RuntimeError("fail")),
            log_label="trunc",
        )

    assert results == []
    assert any(
        r.name == _FallbackAgent.__module__ and "trunc" in r.message and ("x" * 50) in r.message
        for r in caplog.records
    )
    assert not any(("x" * 80) in r.message for r in caplog.records)


def test_fallback_empty_parse_uses_empty_recommendations_when_missing():
    agent = _FallbackAgent()
    payload = agent._fallback_empty_parse()

    assert payload.tier == "empty_parse"
    assert payload.recommendations == ["empty-rec"]
    assert payload.summary == "default-summary"


def test_fallback_empty_parse_applies_empty_summary_override():
    agent = _FallbackAgent()
    payload = agent._fallback_empty_parse(recommendations=["kept"], summary="")

    assert payload.recommendations == ["kept"]
    assert payload.summary == "empty-override"


def test_fallback_empty_parse_preserves_explicit_nonempty_summary():
    agent = _FallbackAgent()
    payload = agent._fallback_empty_parse(summary="from-model")

    assert payload.recommendations == ["empty-rec"]
    assert payload.summary == "from-model"


def test_fallback_empty_parse_empty_recommendations_sequence_uses_class_attr():
    agent = _FallbackAgent()
    payload = agent._fallback_empty_parse(recommendations=[])

    assert payload.recommendations == ["empty-rec"]


def test_call_with_single_fallback_default_log_label(caplog):
    import logging

    agent = _FallbackAgent()

    with caplog.at_level(logging.WARNING):
        status, payload = agent._call_with_single_fallback(
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )

    assert status == "error"
    assert payload.tier == "call_error"
    assert any(
        r.name == _FallbackAgent.__module__
        and _FallbackAgent.__name__ in r.message
        and "boom" in r.message
        for r in caplog.records
    )


def test_call_partial_tolerant_default_log_label(caplog):
    import logging

    agent = _FallbackAgent()

    with caplog.at_level(logging.WARNING):
        results = agent._call_partial_tolerant(
            ["bad"],
            lambda _item: (_ for _ in ()).throw(ValueError("nope")),
        )

    assert results == []
    assert any(
        r.name == _FallbackAgent.__module__
        and _FallbackAgent.__name__ in r.message
        and "nope" in r.message
        for r in caplog.records
    )


def test_fallback_empty_parse_preserves_falsy_summary_when_override_is_none():
    class EmptyOverrideNone(LlmToolAgentBase):
        default_summary = ""
        empty_summary_override = None

    payload = EmptyOverrideNone()._fallback_empty_parse(summary="")

    assert payload.tier == "empty_parse"
    assert payload.summary == ""


def test_fallback_empty_parse_empty_class_recommendations_when_missing():
    class NoEmptyRecs(LlmToolAgentBase):
        empty_recommendations = []

    payload = NoEmptyRecs()._fallback_empty_parse()

    assert payload.recommendations == []
