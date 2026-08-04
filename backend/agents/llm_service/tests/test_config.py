"""Unit tests for llm_service config resolution."""

import pytest

from llm_service import config
from llm_service.attribution import llm_attribution


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
    assert config.resolve_model("backend") == "kimi-k2.7-code:cloud"


def test_resolve_agent_default_think_code_review_is_reduced() -> None:
    """code_review pins the reduced ``high`` tier so its first call avoids the
    max-tier reasoning-only failure mode."""
    assert config.resolve_agent_default_think("code_review") == "high"
    # The pinned tier must be a real registered level for its model, or the wire
    # resolution would fall back to max and defeat the point.
    assert "high" in config.KNOWN_MODEL_THINKING_LEVELS["deepseek-v4-pro:cloud"]


def test_resolve_model_code_review_verify_agent_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """code_review_verify has its own, genuinely lighter AGENT_DEFAULT_MODELS
    entry, independent of (and not affecting) code_review's.

    deepseek-v4-flash:cloud is required here rather than a thinking-tier pin on the same model
    as code_review: deepseek-v4-pro:cloud's reasoning_effort wire mapping collapses
    "low"/"medium" onto the same "high" tier code_review already pinned (see
    KNOWN_MODEL_THINKING_LEVELS), so that route could not be made genuinely
    lighter via AGENT_DEFAULT_THINK alone.
    """
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL_code_review_verify", raising=False)
    monkeypatch.delenv("LLM_MODEL_code_review", raising=False)
    assert config.resolve_model("code_review_verify") == "deepseek-v4-flash:cloud"
    assert config.resolve_model("code_review") == "kimi-k2.7-code:cloud"


def test_resolve_agent_default_think_code_review_verify_has_no_pin() -> None:
    """code_review_verify's model (deepseek-v4-flash:cloud) registers no thinking levels, so it
    has no AGENT_DEFAULT_THINK entry — the pin would be silently inert. code_review's
    own resolution is unaffected by the new key."""
    assert config.resolve_agent_default_think("code_review_verify") is None
    assert "deepseek-v4-flash:cloud" not in config.KNOWN_MODEL_THINKING_LEVELS
    assert config.resolve_agent_default_think("code_review") == "high"


def test_resolve_agent_default_think_unlisted_and_none_are_none() -> None:
    """Unlisted agents (and a None key) get no override — the model's platform
    default tier is left intact."""
    assert config.resolve_agent_default_think("backend") is None
    assert config.resolve_agent_default_think(None) is None
    assert config.resolve_agent_default_think("") is None


def test_resolve_base_url_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    assert config.resolve_base_url() == "https://ollama.com"


def test_resolve_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_TIMEOUT", raising=False)
    assert config.resolve_timeout() == 3600.0


def test_resolve_max_output_tokens_unset_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    assert config.resolve_max_output_tokens() == 0


def test_resolve_max_output_tokens_valid_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "4096")
    assert config.resolve_max_output_tokens() == 4096


def test_resolve_max_output_tokens_malformed_or_nonpositive_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage, zero, and negative all collapse to the 0 'unset' sentinel so a
    client falls through to its provider default instead of a tiny/0 cap."""
    for raw in ("not-an-int", "0", "-5"):
        monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", raw)
        assert config.resolve_max_output_tokens() == 0


def test_resolve_max_output_tokens_ignores_legacy_env_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hard cutover: LLM_MAX_TOKENS must not be consulted."""
    monkeypatch.delenv("LLM_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.setenv("LLM_MAX_TOKENS", "4096")
    assert config.resolve_max_output_tokens() == 0


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


def test_agent_pin_applies_for_registered_model(clean_thinking_env) -> None:
    """An agent in AGENT_DEFAULT_THINK (code_review) resolves think=None to its
    pinned tier for a model that registers the level."""
    with llm_attribution(agent_key="code_review"):
        assert config.resolve_think_for_model("deepseek-v4-pro:cloud", None) == "high"


def test_agent_pin_dropped_for_unregistered_model(clean_thinking_env) -> None:
    """The pin is never put on the wire for a model that does not register the
    level: it falls back to that model's safe default (plain boolean think),
    instead of sending an unvalidated reasoning_effort guess."""
    with llm_attribution(agent_key="code_review"):
        assert config.resolve_think_for_model("deepseek-v4-flash:cloud", None) is True


def test_agent_pin_defers_to_enable_thinking_kill_switch(monkeypatch) -> None:
    """LLM_ENABLE_THINKING=false (the global kill switch) outranks the pin, so an
    operator can still disable thinking for a pinned agent."""
    monkeypatch.delenv("LLM_THINKING_LEVEL", raising=False)
    monkeypatch.setenv("LLM_ENABLE_THINKING", "false")
    with llm_attribution(agent_key="code_review"):
        assert config.resolve_think_for_model("deepseek-v4-pro:cloud", None) is False


def test_agent_pin_defers_to_level_override(monkeypatch) -> None:
    """LLM_THINKING_LEVEL (the operator level override) outranks the pin."""
    monkeypatch.delenv("LLM_ENABLE_THINKING", raising=False)
    monkeypatch.setenv("LLM_THINKING_LEVEL", "low")
    with llm_attribution(agent_key="code_review"):
        assert config.resolve_think_for_model("deepseek-v4-pro:cloud", None) == "low"


def test_no_agent_pin_for_unlisted_agent_or_no_attribution(clean_thinking_env) -> None:
    """An agent with no pin — or no attribution at all — resolves to the model's
    platform-default (max) tier, unchanged by the pin machinery."""
    with llm_attribution(agent_key="backend"):
        assert config.resolve_think_for_model("deepseek-v4-pro:cloud", None) == "max"
    assert config.resolve_think_for_model("deepseek-v4-pro:cloud", None) == "max"


def test_agent_pin_only_replaces_the_none_default(clean_thinking_env) -> None:
    """An explicit caller think value still wins over the pin (the pin is only a
    replacement for the None default)."""
    with llm_attribution(agent_key="code_review"):
        assert config.resolve_think_for_model("deepseek-v4-pro:cloud", "max") == "max"
        assert config.resolve_think_for_model("deepseek-v4-pro:cloud", False) is False


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
