"""Tests for Claude-related resolution in llm_service.config."""

from __future__ import annotations

import pytest

from llm_service import config as c


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """Clear Postgres + LLM_* env so env vars are the sole config source per test."""
    # No Postgres -> runtime config is empty; env vars are the sole source.
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    for var in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_CLAUDE_API_KEY",
        "ANTHROPIC_API_KEY",
        "LLM_CONTEXT_SIZE",
        "OLLAMA_API_KEY",
        "LLM_OLLAMA_API_KEY",
        "LLM_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def test_provider_claude_and_anthropic_alias(monkeypatch):
    """LLM_PROVIDER claude/anthropic (any case) all resolve to 'claude'."""
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    assert c.resolve_provider() == "claude"
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert c.resolve_provider() == "claude"
    monkeypatch.setenv("LLM_PROVIDER", "Claude")  # case-insensitive
    assert c.resolve_provider() == "claude"


def test_provider_defaults_to_ollama():
    """resolve_provider defaults to ollama when nothing is configured."""
    assert c.resolve_provider() == "ollama"


def test_resolve_claude_model_precedence(monkeypatch):
    """Claude model precedence: per-agent env > global env > default."""
    # default
    assert c.resolve_claude_model(None) == c.DEFAULT_CLAUDE_MODEL
    # global
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
    assert c.resolve_claude_model(None) == "claude-sonnet-4-6"
    # per-agent override wins
    monkeypatch.setenv("LLM_MODEL_backend", "claude-haiku-4-5")
    assert c.resolve_claude_model("backend") == "claude-haiku-4-5"
    assert c.resolve_claude_model("frontend") == "claude-sonnet-4-6"


def test_resolve_claude_model_ignores_ollama_agent_defaults():
    """Claude resolution never consults the Ollama AGENT_DEFAULT_MODELS table."""
    # 'backend' has an Ollama default in AGENT_DEFAULT_MODELS; Claude must skip it.
    assert c.resolve_claude_model("backend") == c.DEFAULT_CLAUDE_MODEL


def test_resolve_claude_model_skips_non_claude_env(monkeypatch):
    """A non-Claude LLM_MODEL env value is skipped (incl. the already-warned lock path)."""
    # A non-Claude value in the SHARED LLM_MODEL env (its default is a non-Claude
    # model) is ignored, not sent to Anthropic; a second call hits the "already
    # warned" branch (lock path). The provider-specific runtime key is empty here.
    monkeypatch.setattr(c, "_runtime", lambda key: "")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro:cloud")
    c._warned_non_claude_models.discard("deepseek-v4-pro:cloud")
    assert c.resolve_claude_model(None) == c.DEFAULT_CLAUDE_MODEL
    assert c.resolve_claude_model(None) == c.DEFAULT_CLAUDE_MODEL


def test_resolve_claude_model_trusts_runtime_value(monkeypatch):
    """The provider-specific runtime claude_model is trusted as-is (no heuristic filter)."""
    # The provider-specific runtime claude_model is trusted as-is (no heuristic),
    # so a free-typed custom/gateway Claude model is honored even when it does not
    # match _looks_like_claude_model.
    monkeypatch.setattr(
        c, "_runtime", lambda key: "opus-prod-alias" if key == "claude_model" else ""
    )
    assert c.resolve_claude_model(None) == "opus-prod-alias"


def test_resolve_model_reads_runtime(monkeypatch):
    """The Ollama path honors the runtime model, ranked above global env but below per-agent."""
    # The Ollama path honors the runtime (UI) model, ranked above LLM_MODEL.
    monkeypatch.setattr(c, "_runtime", lambda key: "llama3.2" if key == "ollama_model" else "")
    assert c.resolve_model(None) == "llama3.2"
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro:cloud")
    assert c.resolve_model(None) == "llama3.2"  # runtime still wins over global env
    monkeypatch.setenv("LLM_MODEL_backend", "qwen3-coder:480b-cloud")
    assert c.resolve_model("backend") == "qwen3-coder:480b-cloud"  # per-agent env wins


def test_resolve_model_falls_back_without_runtime(monkeypatch):
    """Without runtime, resolve_model uses the default fallback then global env."""
    monkeypatch.setattr(c, "_runtime", lambda key: "")
    assert c.resolve_model(None) == c.DEFAULT_FALLBACK_MODEL
    monkeypatch.setenv("LLM_MODEL", "glm-5.2:cloud")
    assert c.resolve_model(None) == "glm-5.2:cloud"


def test_resolve_model_uses_provider_specific_runtime_keys(monkeypatch):
    """Each provider's runtime model key is isolated: no cross-provider leakage."""
    # Per-provider keys: resolve_model reads ONLY ollama_model and
    # resolve_claude_model reads ONLY claude_model, so a value stored for one
    # provider can never leak into the other (no heuristic filtering needed).
    runtime = {"ollama_model": "llama3.2", "claude_model": "claude-opus-4-8"}
    monkeypatch.setattr(c, "_runtime", lambda key: runtime.get(key, ""))
    assert c.resolve_model(None) == "llama3.2"
    assert c.resolve_claude_model(None) == "claude-opus-4-8"


def test_resolve_model_for_provider_ollama_uses_runtime(monkeypatch):
    """The chokepoint routes the ollama provider to resolve_model (Ollama key)."""
    # The chokepoint routes ollama -> resolve_model, which reads the Ollama key.
    runtime = {"provider": "ollama", "ollama_model": "llama3.2"}
    monkeypatch.setattr(c, "_runtime", lambda key: runtime.get(key, ""))
    assert c.resolve_model_for_provider(None) == "llama3.2"


def test_claude_model_options_track_context_table():
    """The Claude suggestion list is derived from the context table and includes the default."""
    assert c.CLAUDE_MODEL_SUGGESTIONS == list(c.KNOWN_CLAUDE_CONTEXT.keys())
    assert c.DEFAULT_CLAUDE_MODEL in c.CLAUDE_MODEL_SUGGESTIONS


def test_looks_like_claude_model():
    """_looks_like_claude_model matches Claude/Anthropic ids and rejects others."""
    for m in (
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-fable-5",
        "claude-mythos-5",
        "anthropic.claude-opus-4-8",
        # "anthropic" appearing anywhere also matches (not only as a prefix).
        "my-gateway-anthropic-opus",
    ):
        assert c._looks_like_claude_model(m) is True
    for m in ("deepseek-v4-pro:cloud", "glm-5.2:cloud", "qwen3-coder:480b-cloud", "", "gpt-4"):
        assert c._looks_like_claude_model(m) is False


def test_resolve_claude_model_ignores_non_claude_global_env(monkeypatch):
    """A non-Claude global LLM_MODEL falls back to the Claude default, never sent to Anthropic."""
    # The shipped docker default LLM_MODEL=deepseek-v4-pro:cloud must NOT be sent
    # to the Anthropic API — a non-Claude model falls back to the default.
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro:cloud")
    assert c.resolve_claude_model(None) == c.DEFAULT_CLAUDE_MODEL


def test_resolve_claude_model_ignores_non_claude_per_agent_env(monkeypatch):
    """A non-Claude per-agent LLM_MODEL_<agent> falls back to the Claude default."""
    monkeypatch.setenv("LLM_MODEL_backend", "glm-5.2:cloud")
    assert c.resolve_claude_model("backend") == c.DEFAULT_CLAUDE_MODEL


def test_resolve_claude_model_accepts_claude_global_env(monkeypatch):
    """A Claude global LLM_MODEL is honored by resolve_claude_model."""
    monkeypatch.setenv("LLM_MODEL", "claude-haiku-4-5")
    assert c.resolve_claude_model(None) == "claude-haiku-4-5"


def test_resolve_claude_model_warns_once_per_candidate(monkeypatch, caplog):
    """A non-Claude model warns at most once per distinct value, not per call."""
    # A non-Claude LLM_MODEL under the Claude provider must warn at most once per
    # distinct value, not on every call (resolve runs per get_client()).
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro:cloud")
    c._warned_non_claude_models.discard("deepseek-v4-pro:cloud")
    with caplog.at_level("WARNING"):
        assert c.resolve_claude_model(None) == c.DEFAULT_CLAUDE_MODEL
        assert c.resolve_claude_model(None) == c.DEFAULT_CLAUDE_MODEL
        assert c.resolve_claude_model(None) == c.DEFAULT_CLAUDE_MODEL
    warnings = [r for r in caplog.records if "Ignoring non-Claude model" in r.getMessage()]
    assert len(warnings) == 1


def test_resolve_model_for_provider_dispatches(monkeypatch):
    """resolve_model_for_provider dispatches by active provider (ollama vs claude)."""
    # Ollama provider -> resolve_model; Claude provider -> resolve_claude_model.
    monkeypatch.setenv("LLM_MODEL", "glm-5.2:cloud")
    assert c.resolve_provider() == "ollama"
    assert c.resolve_model_for_provider(None) == "glm-5.2:cloud"

    monkeypatch.setenv("LLM_PROVIDER", "claude")
    # glm-5.2:cloud is not a Claude model, so the Claude path falls back to the default.
    assert c.resolve_model_for_provider(None) == c.DEFAULT_CLAUDE_MODEL
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
    assert c.resolve_model_for_provider(None) == "claude-sonnet-4-6"


def test_resolve_claude_api_key_precedence(monkeypatch):
    """Claude API key precedence: LLM_CLAUDE_API_KEY > ANTHROPIC_API_KEY > ''."""
    assert c.resolve_claude_api_key() == ""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic")
    assert c.resolve_claude_api_key() == "sk-anthropic"
    monkeypatch.setenv("LLM_CLAUDE_API_KEY", "sk-llm")  # Khala-namespaced wins
    assert c.resolve_claude_api_key() == "sk-llm"


def test_resolve_claude_context_size(monkeypatch):
    """Claude context size: known windows, default for unknown, env override wins."""
    assert c.resolve_claude_context_size("claude-opus-4-8") == 1_000_000
    assert c.resolve_claude_context_size("claude-haiku-4-5") == 200_000
    assert c.resolve_claude_context_size("unknown-model") == c.DEFAULT_CLAUDE_CONTEXT
    monkeypatch.setenv("LLM_CONTEXT_SIZE", "50000")
    assert c.resolve_claude_context_size("claude-opus-4-8") == 50_000


def test_resolve_ollama_api_key_precedence(monkeypatch):
    """Ollama API key precedence: OLLAMA_API_KEY > LLM_OLLAMA_API_KEY > ''."""
    assert c.resolve_ollama_api_key() == ""
    monkeypatch.setenv("LLM_OLLAMA_API_KEY", "ll")
    assert c.resolve_ollama_api_key() == "ll"
    monkeypatch.setenv("OLLAMA_API_KEY", "primary")
    assert c.resolve_ollama_api_key() == "primary"


def test_summary_for_claude(monkeypatch):
    """The config summary reports provider/model for Claude and never leaks keys."""
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    summary = c.get_llm_config_summary()
    assert "provider=claude" in summary
    assert "model=" in summary
    # never leak keys
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    assert "sk-secret" not in c.get_llm_config_summary()
