"""Unit tests for :mod:`agent_studio.assistant`.

A scripted completion fn replaces the live LLM so the parsing/merge contract is
exercised deterministically.
"""

from __future__ import annotations

import pytest

from agent_studio.assistant import (
    AgentDesignerAgent,
    _merge_definition,
    _parse_agent_block,
    _parse_suggestions,
    _strip_code_blocks,
)
from agent_studio.models import AgentDefinition


def _completion(text: str):
    """Return a CompleteFn that always yields ``text`` (records the prompts)."""
    seen: dict[str, str] = {}

    def complete(system_prompt: str, prompt: str) -> str:
        seen["system"] = system_prompt
        seen["prompt"] = prompt
        return text

    return complete, seen


_FULL_REPLY = """\
Here's a draft for you.

```agent
{
  "name": "blogging.planner",
  "role": "Plans blog outlines",
  "tags": ["content"],
  "tools": ["web.search"],
  "system_prompt": "You plan outlines."
}
```

```suggestions
["Add a word_count input?", "Target an industry?"]
```
"""


def test_parse_agent_block_extracts_object() -> None:
    block = _parse_agent_block(_FULL_REPLY)
    assert block is not None
    assert block["name"] == "blogging.planner"


def test_parse_agent_block_none_when_absent() -> None:
    assert _parse_agent_block("just prose, no block") is None


def test_parse_agent_block_none_on_bad_json() -> None:
    assert _parse_agent_block("```agent\n{not json}\n```") is None


def test_parse_agent_block_none_when_not_object() -> None:
    assert _parse_agent_block('```agent\n["a", "b"]\n```') is None


def test_parse_suggestions_extracts_list() -> None:
    assert _parse_suggestions(_FULL_REPLY) == ["Add a word_count input?", "Target an industry?"]


def test_parse_suggestions_empty_when_absent() -> None:
    assert _parse_suggestions("no block here") == []


def test_parse_suggestions_empty_on_bad_json() -> None:
    assert _parse_suggestions("```suggestions\n{bad}\n```") == []


def test_parse_suggestions_empty_when_not_list() -> None:
    assert _parse_suggestions('```suggestions\n{"a": 1}\n```') == []


def test_strip_code_blocks_removes_both() -> None:
    stripped = _strip_code_blocks(_FULL_REPLY)
    assert "```" not in stripped
    assert stripped.startswith("Here's a draft")


def test_merge_definition_overlays_and_preserves_server_fields() -> None:
    current = AgentDefinition(mode="refine", cloned_from="src.id", name="old")
    merged = _merge_definition(
        current, {"name": "new", "role": "r", "mode": "new", "cloned_from": "evil"}
    )
    assert merged.name == "new"
    assert merged.role == "r"
    # Server-owned fields are never rewritten by the model.
    assert merged.mode == "refine"
    assert merged.cloned_from == "src.id"


def test_merge_definition_ignores_unknown_keys() -> None:
    merged = _merge_definition(AgentDefinition(), {"name": "n", "bogus": 123})
    assert merged.name == "n"
    assert not hasattr(merged, "bogus")


def test_respond_new_mode_parses_and_merges() -> None:
    complete, seen = _completion(_FULL_REPLY)
    agent = AgentDesignerAgent(complete=complete)
    reply, updated, suggestions = agent.respond([], AgentDefinition(mode="new"), "Build a planner")

    assert "```" not in reply
    assert updated is not None
    assert updated.name == "blogging.planner"
    assert updated.mode == "new"
    assert suggestions == ["Add a word_count input?", "Target an industry?"]
    assert "NEW agent" in seen["system"]


def test_respond_refine_mode_uses_refine_prefix_and_preserves_clone() -> None:
    complete, seen = _completion(_FULL_REPLY)
    agent = AgentDesignerAgent(complete=complete)
    current = AgentDefinition(mode="refine", cloned_from="src.id", name="orig", role="r")
    reply, updated, _ = agent.respond(
        [("user", "hi"), ("assistant", "hello")], current, "rename it"
    )

    assert "REFINING" in seen["system"]
    assert updated is not None
    assert updated.mode == "refine"
    assert updated.cloned_from == "src.id"
    # History + current definition are threaded into the prompt.
    assert "Current agent definition" in seen["prompt"]
    assert "user: hi" in seen["prompt"]
    assert "rename it" in seen["prompt"]


def test_respond_no_block_returns_none_definition() -> None:
    complete, _ = _completion("Just a clarifying question, no draft yet.")
    agent = AgentDesignerAgent(complete=complete)
    reply, updated, suggestions = agent.respond([], AgentDefinition(), "hmm")
    assert updated is None
    assert suggestions == []
    assert reply == "Just a clarifying question, no draft yet."


def test_respond_rejects_empty_message() -> None:
    agent = AgentDesignerAgent(complete=_completion("x")[0])
    with pytest.raises(AssertionError):
        agent.respond([], AgentDefinition(), "")


def test_build_prompt_omits_definition_when_empty() -> None:
    prompt = AgentDesignerAgent._build_prompt([], AgentDefinition(), "go")
    assert "Current agent definition" not in prompt
    assert prompt.endswith("user: go")
