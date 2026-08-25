"""Tests for ``llm_service.strands_model.model_fingerprint``.

The canonical attribute-probing tail for deriving a stable identity string
from an already-resolved Strands model. ``code_review_agent.mapping``'s
``_review_model_fingerprint`` and ``code_review_agent.transcript.model_label``
both delegate to this helper; their own tests (``test_code_review_cache.py``)
cover their extra client-resolution/fallback behavior, not this tail.
"""

from __future__ import annotations

from llm_service.strands_model import model_fingerprint, run_strands_agent


class _Bare:
    pass


def test_model_fingerprint_prefers_model_id_attr() -> None:
    class _Model:
        model_id = "claude-x"

    assert model_fingerprint(_Model()) == "claude-x"


def test_model_fingerprint_falls_back_to_model_name_then_model_attr() -> None:
    class _NameOnly:
        model_name = "gpt-y"

    class _ModelAttrOnly:
        model = "ollama-z"

    assert model_fingerprint(_NameOnly()) == "gpt-y"
    assert model_fingerprint(_ModelAttrOnly()) == "ollama-z"


def test_model_fingerprint_ignores_non_string_attrs() -> None:
    class _NonString:
        model_id = 123
        model_name = "the-real-one"

    assert model_fingerprint(_NonString()) == "the-real-one"


def test_model_fingerprint_ignores_empty_string_attrs() -> None:
    class _EmptyId:
        model_id = ""
        model_name = "named-model"

    assert model_fingerprint(_EmptyId()) == "named-model"


def test_model_fingerprint_falls_back_to_config_dict() -> None:
    class _ConfigOnly:
        config = {"model_id": "cfg-model"}

    assert model_fingerprint(_ConfigOnly()) == "cfg-model"


def test_model_fingerprint_config_dict_falls_back_through_model_name_then_model() -> None:
    """Within ``.config``, a non-string/empty ``model_id`` is skipped in favor
    of ``model_name`` then ``model`` — same probing order as the top-level
    attributes, not a first-match-wins ``or`` chain."""

    class _ConfigFallback:
        config = {"model_id": 123, "model_name": "", "model": "mdl"}

    assert model_fingerprint(_ConfigFallback()) == "mdl"


def test_model_fingerprint_ignores_non_dict_config() -> None:
    class _NonDictConfig:
        config = "not-a-dict"
        model_name = "real-name"

    assert model_fingerprint(_NonDictConfig()) == "real-name"


def test_model_fingerprint_falls_back_to_type_name() -> None:
    assert model_fingerprint(_Bare()) == "_Bare"
    assert model_fingerprint(object()) == "object"


def test_model_fingerprint_survives_attribute_that_raises() -> None:
    """A raising descriptor/property must fall back to the type name, not
    propagate -- ``model_fingerprint`` promises 'Never raises'."""

    class _Angry:
        @property
        def model_id(self) -> str:
            raise RuntimeError("no touching")

    assert model_fingerprint(_Angry()) == "_Angry"


def test_model_fingerprint_survives_config_that_raises() -> None:
    """A raising ``.config`` descriptor is swallowed the same way."""

    class _AngryConfig:
        @property
        def config(self) -> dict:
            raise RuntimeError("no touching")

    assert model_fingerprint(_AngryConfig()) == "_AngryConfig"


class _RecordingAgentFactory:
    """Callable ``Agent`` stand-in: records the kwargs it is built with."""

    def __init__(self) -> None:
        self.build_kwargs: list[dict] = []

    def __call__(self, **kwargs):
        self.build_kwargs.append(kwargs)

        def _run(prompt: str) -> str:
            return f"resp:{prompt}"

        return _run


def test_run_strands_agent_omits_system_prompt_by_default() -> None:
    """No system_prompt_content -> agent built exactly as before this kwarg existed."""
    factory = _RecordingAgentFactory()
    model = object()

    result = run_strands_agent(factory, model, "hello")

    assert result == "resp:hello"
    assert factory.build_kwargs == [{"model": model}]


def test_run_strands_agent_passes_system_prompt_content_as_system_prompt() -> None:
    factory = _RecordingAgentFactory()
    model = object()
    content = ["shared system segment"]

    result = run_strands_agent(factory, model, "hello", system_prompt_content=content)

    assert result == "resp:hello"
    assert factory.build_kwargs == [{"model": model, "system_prompt": content}]


def test_run_strands_agent_omits_system_prompt_when_content_is_empty_list() -> None:
    """An empty (falsy) list is treated the same as None -- no system_prompt kwarg."""
    factory = _RecordingAgentFactory()
    model = object()

    run_strands_agent(factory, model, "hello", system_prompt_content=[])

    assert factory.build_kwargs == [{"model": model}]
