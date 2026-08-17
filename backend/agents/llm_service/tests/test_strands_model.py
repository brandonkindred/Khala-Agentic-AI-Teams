"""Tests for ``llm_service.strands_model.model_fingerprint``.

The canonical attribute-probing tail for deriving a stable identity string
from an already-resolved Strands model. ``code_review_agent.mapping``'s
``_review_model_fingerprint`` and ``code_review_agent.transcript.model_label``
both delegate to this helper; their own tests (``test_code_review_cache.py``)
cover their extra client-resolution/fallback behavior, not this tail.
"""

from __future__ import annotations

from llm_service.strands_model import model_fingerprint


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
