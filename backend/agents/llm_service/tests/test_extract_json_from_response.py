"""Characterization tests: extract_json_from_response vs. agent_call_json.

Part of epic #6088 (consolidating two overlapping LLM JSON-salvage layers).
``extract_json_from_response`` (this package, ``llm_service.util``) is the
decided canonical helper; ``agent_call_json`` (``shared.llm_recovery``, which
delegates to the more sophisticated ``extract_json_object``/``_salvage_object``
engine) is the richer of the two implementations it is meant to absorb. This
module runs both against a shared fixture corpus and records, per fixture,
where they agree and where they diverge. Every result below was captured by
actually running both functions, not inferred from reading their source.

``extract_json_from_response`` now closes its recovery gap against
``agent_call_json`` by falling back, as a last resort, to the same shared
``extract_json_object`` salvage engine ``agent_call_json`` uses -- so the four
capability gaps this corpus originally documented are now resolved:

  1. Truncation repair -- fabricating a missing closing bracket/brace for an
     object cut off mid-stream (e.g. by a max-tokens limit). See
     ``truncated_json``.
  2. ``<think>``/``<thinking>``/``<reasoning>``/``<json>`` tag stripping
     before salvage. See ``think_block_wrapped``.
  3. Envelope descent -- when a keys-anchor is supplied and the top-level
     object doesn't carry it, looking one level into that object's
     dict-valued children for a match (e.g. unwrapping
     ``{"result": {"tasks": [...]}}`` when anchored on ``"tasks"``). See
     ``envelope_wrapped``.
  4. A recall/disambiguation strategy for "echoed format example, then the
     real payload" prompts, via a balanced, string-aware scanner whose "last
     accepted candidate wins" heuristic selects the real (trailing) object
     instead of splicing the decoy and the real object together. See
     ``format_echo_before_payload``.

One documented, accepted difference remains:

  5. A domain-specific failure signal. On genuinely unrecoverable input (see
     ``unrecoverable_prose``) both refuse to fabricate a result, but
     extract_json_from_response raises a purpose-built ``LLMJsonParseError``
     (with a response preview attached) while agent_call_json re-raises the
     generic stdlib ``json.JSONDecodeError`` it started from -- a caller
     pattern-matching on exception type must handle both today. This is out
     of scope: fixing it would change extract_json_from_response's existing,
     widely-relied-on failure contract rather than purely add capability.

Where they agree: every case in this corpus (see ``EXPECTED`` below, and
``test_agreement_documented`` for value-level confirmation).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import pytest

from llm_service.interface import LLMJsonParseError
from llm_service.util import extract_json_from_response
from shared.llm_recovery import agent_call_json


@dataclass(frozen=True)
class Case:
    name: str
    text: str
    note: str  # one-line human description, echoed in failure output
    anchor_keys: Optional[frozenset] = None  # threaded as expected_keys / required_keys


CASES: list[Case] = [
    Case(
        "clean_object",
        '{"tasks": [{"id": "t1"}]}',
        "baseline: both parse identically",
    ),
    Case(
        "fenced_json",
        '```json\n{"ok": true}\n```',
        "```json fence -- both strip it",
    ),
    Case(
        "prose_wrapped",
        'Sure! Here is the result: {"edits": []} — hope that helps',
        "prose-wrapped JSON -- both recover via regex/scan",
    ),
    Case(
        "trailing_commentary",
        '{"approved": true, "issues": []}\nLet me know if you need anything else.',
        "valid JSON followed by trailing prose commentary",
    ),
    Case(
        "trailing_comma",
        '{"tasks": [{"id": "t1"}],}',
        "trailing comma -- both have dedicated repair for this",
    ),
    Case(
        "truncated_json",
        'Here is the plan: {"tasks": [{"id": "t1"}, {"id": "t2"',
        "truncated/unclosed JSON -- only agent_call_json fabricates a closing bracket",
    ),
    Case(
        "think_block_wrapped",
        '<think>the schema might look like {"maybe": true} but let us see</think>\n{"ok": true}',
        "brace inside a <think> block -- only agent_call_json strips reasoning tags first",
    ),
    Case(
        "envelope_wrapped",
        'Note: format looks like {"format": {"a": 1}} but the real answer is '
        '{"result": {"tasks": [{"id": "t1"}]}}',
        "payload nested under an envelope key, anchored on 'tasks' -- only agent_call_json descends",
        anchor_keys=frozenset({"tasks"}),
    ),
    Case(
        "brace_in_string_value",
        '{"reason": "use } to close the block", "approved": false}',
        "brace character inside a string value -- both handle this correctly",
    ),
    Case(
        "format_echo_before_payload",
        'Format: {"approved": true, "issues": []}\n'
        'Verdict: {"approved": false, "issues": ["missing tests"]}',
        "an echoed format example precedes the real payload -- only agent_call_json disambiguates",
    ),
    Case(
        "unrecoverable_prose",
        "I cannot complete this request because the file was not found.",
        "genuinely no JSON present -- both refuse to fabricate, but raise different exception types",
    ),
]


@dataclass(frozen=True)
class Outcome:
    ok: bool
    value: Optional[dict[str, Any]] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None


def _run_extract_json_from_response(case: Case) -> Outcome:
    try:
        return Outcome(
            ok=True, value=extract_json_from_response(case.text, expected_keys=case.anchor_keys)
        )
    except LLMJsonParseError as exc:
        return Outcome(ok=False, error_type="LLMJsonParseError", error_message=str(exc))


class _FakeAgent:
    """Minimal Strands-style callable agent stub: ``agent(prompt) -> reply``."""

    def __init__(self, reply: str) -> None:
        self._reply = reply

    def __call__(self, prompt: str) -> str:
        return self._reply


def _run_agent_call_json(case: Case) -> Outcome:
    try:
        return Outcome(
            ok=True,
            value=agent_call_json(_FakeAgent(case.text), "prompt", required_keys=case.anchor_keys),
        )
    except json.JSONDecodeError as exc:
        return Outcome(ok=False, error_type="JSONDecodeError", error_message=str(exc))


# name -> (extract_json_from_response should succeed?, agent_call_json should succeed?)
# Every entry here was verified by actually running both functions against the
# corresponding Case, not predicted from reading the source.
EXPECTED: dict[str, tuple[bool, bool]] = {
    "clean_object": (True, True),
    "fenced_json": (True, True),
    "prose_wrapped": (True, True),
    "trailing_commentary": (True, True),
    "trailing_comma": (True, True),
    "truncated_json": (True, True),
    "think_block_wrapped": (True, True),
    "envelope_wrapped": (True, True),
    "brace_in_string_value": (True, True),
    "format_echo_before_payload": (True, True),
    "unrecoverable_prose": (False, False),
}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_extract_json_from_response_outcome(case: Case) -> None:
    expected_ok, _ = EXPECTED[case.name]
    outcome = _run_extract_json_from_response(case)
    assert outcome.ok is expected_ok, (
        f"{case.name}: {case.note} -- extract_json_from_response "
        f"ok={outcome.ok} (expected {expected_ok}); {outcome.error_message}"
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_agent_call_json_outcome(case: Case) -> None:
    _, expected_ok = EXPECTED[case.name]
    outcome = _run_agent_call_json(case)
    assert outcome.ok is expected_ok, (
        f"{case.name}: {case.note} -- agent_call_json "
        f"ok={outcome.ok} (expected {expected_ok}); {outcome.error_message}"
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_agreement_documented(case: Case) -> None:
    """Where both functions succeed on the same case, do they also agree on
    the resulting dict? Divergent-success cases are expected and documented
    in the module docstring, not treated as failures here -- this test only
    catches an UNDOCUMENTED value mismatch on a case both claim to handle.
    """
    a = _run_extract_json_from_response(case)
    b = _run_agent_call_json(case)
    if a.ok and b.ok:
        assert a.value == b.value, (
            f"{case.name}: both succeeded but returned different dicts -- "
            f"undocumented divergence: {a.value!r} vs {b.value!r}"
        )
