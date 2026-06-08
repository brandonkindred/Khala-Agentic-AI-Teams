"""Refinement parse-retry resilience.

``RefinementAgent.run`` asks the LLM to emit the complete fixed program as a
JSON string. The model occasionally returns an empty, thinking-only, or
prose-only response with no JSON object. That is not a transport fault — the
fault-tolerance envelope sees a "successful" string — so without a parse-retry
a single such response wastes the whole refinement round (the orchestrator
falls back to the unchanged code).

These tests lock in the recovery: an unparseable response is re-prompted with
the parse error as feedback, and the retry budget
(``STRATEGY_LAB_REFINEMENT_PARSE_RETRIES``) bounds the rare persistent case.
The behaviour mirrors ``DesignAgent._invoke_and_parse``.
"""

from __future__ import annotations

import json
import logging
from typing import List

import pytest

from investment_team.models import RiskLimits, StrategySpec
from investment_team.strategy_lab.agents import refinement as mod
from investment_team.strategy_lab.agents._parse_helpers import (
    build_json_correction_prompt,
    parse_retry_budget,
)
from investment_team.strategy_lab.agents.refinement import RefinementAgent


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-parse-retry",
        authored_by="test",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        risk_limits=RiskLimits(),
    )


class _ScriptedAgent:
    """Strands ``Agent`` replacement returning a scripted payload per call.

    The last payload is repeated once the script is exhausted so a test can
    assert "always unparseable" without enumerating every attempt.
    """

    def __init__(self, payloads: List[str]) -> None:
        self._payloads = payloads
        self.calls = 0

    def __call__(self, _prompt: str) -> str:
        idx = min(self.calls, len(self._payloads) - 1)
        self.calls += 1
        return self._payloads[idx]


_GOOD = '{"strategy_code": "# fixed", "changes_made": "tightened guard"}'


def test_retries_unparseable_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A first no-JSON response is re-prompted; the second (valid) one is used."""
    agent = _ScriptedAgent(["I could not find a fix.", _GOOD])
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(mod, "Agent", lambda **_k: agent)
    monkeypatch.setenv("STRATEGY_LAB_REFINEMENT_PARSE_RETRIES", "2")

    updates, new_code = RefinementAgent().run(
        spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
    )

    assert new_code == "# fixed"
    assert updates == {"changes_made": "tightened guard"}
    assert agent.calls == 2  # one unparseable, one good


def test_retries_malformed_braced_json_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A braced-but-invalid first response (the 'Failed to parse JSON' branch of
    extract_json_object, distinct from 'No JSON object found') is also retried."""
    # Has a '{' so it passes the brace-scan, but is not valid JSON — exercises
    # the json.JSONDecodeError -> ValueError wrapping path.
    agent = _ScriptedAgent(['{"strategy_code": "# half', _GOOD])
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(mod, "Agent", lambda **_k: agent)
    monkeypatch.setenv("STRATEGY_LAB_REFINEMENT_PARSE_RETRIES", "2")

    updates, new_code = RefinementAgent().run(
        spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
    )

    assert new_code == "# fixed"
    assert agent.calls == 2


def test_correction_prompt_is_fed_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry prompt carries the JSON-correction preamble, not the raw task."""
    seen: List[str] = []

    class _RecordingAgent:
        """Single shared stub: the real code builds a fresh ``Agent`` per
        attempt, so call state is tracked here, not on the per-attempt object."""

        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, prompt: str) -> str:
            seen.append(prompt)
            self.calls += 1
            return "no json at all" if self.calls == 1 else _GOOD

    recording = _RecordingAgent()
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(mod, "Agent", lambda **_k: recording)
    monkeypatch.setenv("STRATEGY_LAB_REFINEMENT_PARSE_RETRIES", "2")

    RefinementAgent().run(
        spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
    )

    assert len(seen) == 2
    # First call is the original task; the retry is the correction preamble that
    # quotes the parse error and re-attaches the original task.
    assert "Fix the following trading strategy code" in seen[0]
    assert "could not be parsed as a single JSON object" in seen[1]
    assert "No JSON object found in LLM response" in seen[1]
    assert "Fix the following trading strategy code" in seen[1]


def test_initial_prompt_embeds_response_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first prompt sent to the LLM carries the JSON Schema and an explicit
    JSON-only instruction so the model knows the exact expected response shape."""
    seen: List[str] = []

    class _CapturingAgent:
        def __call__(self, prompt: str) -> str:
            seen.append(prompt)
            return _GOOD

    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(mod, "Agent", lambda **_k: _CapturingAgent())

    RefinementAgent().run(
        spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
    )

    assert len(seen) == 1
    prompt = seen[0]
    # Explicit "respond in JSON" directive.
    assert "Response format — JSON only" in prompt
    assert "MUST conform to this JSON Schema" in prompt
    # The actual schema (same object passed to the model's ``format`` field) is
    # embedded verbatim — not just a loose two-key example.
    assert mod._REFINEMENT_SCHEMA_JSON in prompt
    schema = json.loads(mod._REFINEMENT_SCHEMA_JSON)
    assert schema["type"] == "object"
    assert schema["required"] == ["strategy_code"]
    assert {"strategy_code", "changes_made", "risk_limits"} <= set(schema["properties"])


def test_embedded_schema_matches_format_constraint() -> None:
    """The schema in the prompt is the SAME one fed to the model decoder, so the
    prompt-level contract and the structured-output constraint cannot drift."""
    from investment_team.strategy_lab.agents._response_schemas import REFINEMENT_SCHEMA

    assert json.loads(mod._REFINEMENT_SCHEMA_JSON) == REFINEMENT_SCHEMA


def test_raises_after_budget_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    """With retries disabled, the no-JSON ValueError surfaces on attempt one."""
    agent = _ScriptedAgent(["never json"])
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(mod, "Agent", lambda **_k: agent)
    monkeypatch.setenv("STRATEGY_LAB_REFINEMENT_PARSE_RETRIES", "0")

    with pytest.raises(ValueError, match="No JSON object found"):
        RefinementAgent().run(
            spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
        )
    assert agent.calls == 1


def test_exhausts_full_budget_before_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """An always-unparseable model is retried exactly ``retries + 1`` times."""
    agent = _ScriptedAgent(["nope"])
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(mod, "Agent", lambda **_k: agent)
    monkeypatch.setenv("STRATEGY_LAB_REFINEMENT_PARSE_RETRIES", "2")

    with pytest.raises(ValueError):
        RefinementAgent().run(
            spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
        )
    assert agent.calls == 3  # 2 retries + 1


def test_happy_path_builds_agent_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A first-try valid response makes exactly one model/agent construction —
    the schema-forwarding contract is unchanged when no retry is needed."""
    builds = {"models": 0, "agents": 0}

    def _model(*_a, **_k):
        builds["models"] += 1
        return object()

    def _agent(**_k):
        builds["agents"] += 1
        return _ScriptedAgent([_GOOD])

    monkeypatch.setattr(mod, "get_strands_model", _model)
    monkeypatch.setattr(mod, "Agent", _agent)
    monkeypatch.setenv("STRATEGY_LAB_REFINEMENT_PARSE_RETRIES", "2")

    RefinementAgent().run(
        spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
    )

    assert builds == {"models": 1, "agents": 1}


def test_logs_warning_on_each_unparseable_attempt(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    agent = _ScriptedAgent(["bad", _GOOD])
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(mod, "Agent", lambda **_k: agent)
    monkeypatch.setenv("STRATEGY_LAB_REFINEMENT_PARSE_RETRIES", "2")

    logger_name = "investment_team.strategy_lab.agents.refinement"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        RefinementAgent().run(
            spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
        )

    warnings = [r for r in caplog.records if "unparseable JSON" in r.message]
    assert len(warnings) == 1
    assert "attempt 1/3" in warnings[0].message
    assert "failure_phase=execution" in warnings[0].message


# ---------------------------------------------------------------------------
# Budget resolution
# ---------------------------------------------------------------------------


def test_parse_retries_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRATEGY_LAB_REFINEMENT_PARSE_RETRIES", raising=False)
    assert parse_retry_budget("STRATEGY_LAB_REFINEMENT_PARSE_RETRIES") == 2


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0", 0),
        ("5", 5),
        ("-3", 0),  # sub-zero clamps to 0
        ("not-an-int", 2),  # garbage falls back to default
        ("", 2),  # empty falls back to default
    ],
)
def test_parse_retries_env_parsing(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    monkeypatch.setenv("STRATEGY_LAB_REFINEMENT_PARSE_RETRIES", raw)
    assert parse_retry_budget("STRATEGY_LAB_REFINEMENT_PARSE_RETRIES") == expected


def test_refinement_keys_hint_names_both_output_keys() -> None:
    """Guard the refinement constant itself: if someone edits _CORRECTION_KEYS_HINT
    to drop a key, this fails (the helper-plumbing test below would not, since it
    only proves the hint is echoed)."""
    assert "strategy_code" in mod._CORRECTION_KEYS_HINT
    assert "changes_made" in mod._CORRECTION_KEYS_HINT


def test_build_json_correction_prompt_quotes_error_task_and_hint() -> None:
    """Guard the shared helper's plumbing independently of the constant's content:
    the error, the original task, and a distinct keys_hint marker all flow through."""
    prompt = build_json_correction_prompt(
        "ORIGINAL REFINEMENT TASK",
        ValueError("No JSON object found in LLM response"),
        keys_hint=" SENTINEL_HINT_TOKEN",
    )
    assert "No JSON object found in LLM response" in prompt
    assert "ORIGINAL REFINEMENT TASK" in prompt
    assert "SENTINEL_HINT_TOKEN" in prompt
    # Empty hint (the designer path) must not leave a dangling double space.
    designer = build_json_correction_prompt("T", ValueError("e"))
    assert "commentary.\nEvery brace" in designer
