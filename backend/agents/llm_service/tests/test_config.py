"""Unit tests for llm_service config resolution."""

import pytest

from llm_service import config


def test_resolve_provider_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert config.resolve_provider() == "ollama"


def test_resolve_provider_dummy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    assert config.resolve_provider() == "dummy"


def test_resolve_model_agent_key_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("LLM_MODEL_soc2", "custom-model")
    assert config.resolve_model("soc2") == "custom-model"


def test_resolve_model_global_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "global-model")
    monkeypatch.delenv("LLM_MODEL_soc2", raising=False)
    assert config.resolve_model("soc2") == "global-model"


def test_resolve_model_agent_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL_backend", raising=False)
    assert config.resolve_model("backend") == "deepseek-v4-pro:cloud"


def test_resolve_base_url_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    assert config.resolve_base_url() == "https://ollama.com"


def test_resolve_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_TIMEOUT", raising=False)
    assert config.resolve_timeout() == 900.0


def test_resolve_context_size_for_model_known(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_CONTEXT_SIZE", raising=False)
    assert config.resolve_context_size_for_model("qwen3.5:397b-cloud") == 262144


def test_resolve_context_size_for_default_deepseek_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default agent model resolves deterministically (no /api/show dependency)."""
    monkeypatch.delenv("LLM_CONTEXT_SIZE", raising=False)
    assert config.resolve_context_size_for_model("deepseek-v4-pro:cloud") == 1000000


def test_resolve_context_size_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_CONTEXT_SIZE", "100000")
    assert config.resolve_context_size_for_model("unknown-model") == 100000


# ---------------------------------------------------------------------------
# Thinking-level resolution
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_thinking_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_ENABLE_THINKING", raising=False)
    monkeypatch.delenv("LLM_THINKING_LEVEL", raising=False)


def test_resolve_think_none_upgrades_registered_model_to_max_level(clean_thinking_env) -> None:
    """Platform default: thinking on, at the model's highest registered level
    (deepseek-v4-pro documents reasoning_effort high/max — max is the top)."""
    assert config.resolve_think_for_model("deepseek-v4-pro:cloud", None) == "max"


def test_resolve_think_none_stays_boolean_for_unregistered_model(clean_thinking_env) -> None:
    """Models with no registered levels get plain boolean thinking."""
    assert config.resolve_think_for_model("qwen3.5:cloud", None) is True


def test_resolve_think_explicit_false_stays_off(clean_thinking_env) -> None:
    assert config.resolve_think_for_model("deepseek-v4-pro:cloud", False) is False


def test_resolve_think_explicit_level_passes_through(clean_thinking_env) -> None:
    assert config.resolve_think_for_model("deepseek-v4-pro:cloud", "medium") == "medium"


def test_resolve_think_global_disable_respected_for_default(
    clean_thinking_env, monkeypatch
) -> None:
    monkeypatch.setenv("LLM_ENABLE_THINKING", "false")
    assert config.resolve_think_for_model("deepseek-v4-pro:cloud", None) is False


def test_resolve_think_explicit_true_wins_over_global_disable(
    clean_thinking_env, monkeypatch
) -> None:
    monkeypatch.setenv("LLM_ENABLE_THINKING", "false")
    assert config.resolve_think_for_model("deepseek-v4-pro:cloud", True) == "max"


def test_resolve_think_env_level_override(clean_thinking_env, monkeypatch) -> None:
    monkeypatch.setenv("LLM_THINKING_LEVEL", "medium")
    assert config.resolve_think_for_model("deepseek-v4-pro:cloud", None) == "medium"


def test_resolve_think_env_garbage_falls_back_to_max(clean_thinking_env, monkeypatch) -> None:
    monkeypatch.setenv("LLM_THINKING_LEVEL", "banana")
    assert config.resolve_think_for_model("deepseek-v4-pro:cloud", None) == "max"


def test_resolve_think_env_level_ignored_for_unregistered_model(
    clean_thinking_env, monkeypatch
) -> None:
    """A level string would be rejected by models that only support boolean think."""
    monkeypatch.setenv("LLM_THINKING_LEVEL", "high")
    assert config.resolve_think_for_model("qwen3.5:cloud", None) is True


def test_resolve_think_env_high_selects_documented_lower_level(
    clean_thinking_env, monkeypatch
) -> None:
    monkeypatch.setenv("LLM_THINKING_LEVEL", "high")
    assert config.resolve_think_for_model("deepseek-v4-pro:cloud", None) == "high"


@pytest.mark.parametrize(
    ("model", "think", "expected"),
    [
        # Registered-levels model: step down one level; lowest has nothing below.
        ("deepseek-v4-pro:cloud", "max", "high"),
        ("deepseek-v4-pro:cloud", "high", "medium"),
        ("deepseek-v4-pro:cloud", "medium", "low"),
        ("deepseek-v4-pro:cloud", "low", None),
        # Boolean thinking: True -> False; False is already off.
        ("unknown-model", True, False),
        ("unknown-model", False, None),
        ("deepseek-v4-pro:cloud", True, False),
        # Unregistered level string: disabling reasoning is the only provable change.
        ("unknown-model", "high", False),
        ("deepseek-v4-pro:cloud", "xhigh", False),
    ],
)
def test_downgrade_think(model: str, think: "bool | str", expected: "bool | str | None") -> None:
    assert config.downgrade_think(model, think) == expected


def test_downgrade_think_lowest_level_returns_none_not_false() -> None:
    """The lowest registered level must yield None (no proof of change), not False."""
    assert config.downgrade_think("deepseek-v4-pro:cloud", "low") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, True),
        ("", True),
        ("true", True),
        ("yes", True),
        ("1", True),
        ("garbage", True),
        ("false", False),
        ("FALSE", False),
        (" false ", False),
        ("0", False),
        ("no", False),
        ("No", False),
    ],
)
def test_env_flag_enabled(
    monkeypatch: pytest.MonkeyPatch, raw: "str | None", expected: bool
) -> None:
    """Shared default-on toggle parser: off only for explicit falsy values."""
    if raw is None:
        monkeypatch.delenv("KHALA_TEST_FLAG", raising=False)
    else:
        monkeypatch.setenv("KHALA_TEST_FLAG", raw)
    assert config.env_flag_enabled("KHALA_TEST_FLAG") is expected
