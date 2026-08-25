"""Tech Lead ``_agent_call_json`` parses strict JSON and recovers imperfect output."""

from __future__ import annotations

import json

import pytest

from software_engineering_team.tech_lead_agent.agent import _agent_call_json


class _FakeAgent:
    """Minimal Strands-Agent stand-in: calling it returns a canned response."""

    def __init__(self, response: str) -> None:
        self._response = response

    def __call__(self, prompt: str) -> str:
        return self._response


def test_strict_json_parse() -> None:
    assert _agent_call_json(_FakeAgent('{"a": 1}'), "p") == {"a": 1}


def test_strips_markdown_fence() -> None:
    assert _agent_call_json(_FakeAgent('```json\n{"a": 2}\n```'), "p") == {"a": 2}


def test_recovers_prose_wrapped_json() -> None:
    agent = _FakeAgent('Sure! Here is the plan: {"tasks": [{"id": "t1"}]} Done.')
    assert _agent_call_json(agent, "p") == {"tasks": [{"id": "t1"}]}


def test_recovers_from_think_block() -> None:
    assert _agent_call_json(_FakeAgent('<think>reasoning</think>\n{"ok": true}'), "p") == {
        "ok": True
    }


def test_raises_when_unrecoverable() -> None:
    with pytest.raises(json.JSONDecodeError):
        _agent_call_json(_FakeAgent("there is absolutely no json here"), "p")


def test_required_keys_anchor_skips_usage_echo() -> None:
    """When the call site knows its schema, a trailing usage echo that lacks the
    anchor key must not be returned in place of the real (recoverable) payload."""
    agent = _FakeAgent('Verdict: {"approved": false, "issues": ["x"],}\nUsage: {"tokens": 9}')
    assert _agent_call_json(agent, "p", required_keys=("approved",)) == {
        "approved": False,
        "issues": ["x"],
    }


def test_routes_through_extract_json_from_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_agent_call_json`` must fall back to the canonical ``extract_json_from_response``
    helper (not the older ``agent_call_json``) once the earlier tiers fail — this is the
    migration's contract. ``extract_json_object`` (tier 2) is forced to fail here since it
    already recovers most realistic malformed input on its own, which would otherwise mean
    this input never actually reaches tier 3."""
    import software_engineering_team.tech_lead_agent.agent as tl_mod

    calls: list = []
    raw = "not real json at all"

    monkeypatch.setattr(tl_mod, "extract_json_object", lambda text, required_keys=None: None)

    def fake_extract(text: str, *, expected_keys=None):
        calls.append((text, expected_keys))
        return {"a": 1}

    monkeypatch.setattr(tl_mod, "extract_json_from_response", fake_extract)
    assert _agent_call_json(_FakeAgent(raw), "p", required_keys=("a",)) == {"a": 1}
    assert calls == [(raw, frozenset({"a"}))]


def test_strict_valid_json_bypasses_recovery_tiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A well-formed JSON reply must parse via strict ``json.loads`` and never reach the
    recovery tiers — this guards against ``extract_json_from_response``'s own pre-parse
    heuristics (e.g. its ``---DRAFT---`` shortcut) misfiring on valid payloads whose text
    happens to contain a matching literal substring."""
    import software_engineering_team.tech_lead_agent.agent as tl_mod

    def unexpected(*args, **kwargs):
        raise AssertionError("recovery tiers should not be reached")

    monkeypatch.setattr(tl_mod, "extract_json_object", unexpected)
    monkeypatch.setattr(tl_mod, "extract_json_from_response", unexpected)
    agent = _FakeAgent('{"reason": "discusses the ---DRAFT--- marker"}')
    assert _agent_call_json(agent, "p") == {"reason": "discusses the ---DRAFT--- marker"}


def test_prose_wrapped_json_containing_draft_marker_recovers_via_extract_json_object() -> None:
    """A prose-wrapped (not strictly-parseable) reply whose payload text happens to contain
    the literal ``---DRAFT---`` substring must still recover the real object — via tier 2's
    ``extract_json_object`` (which has no draft-sentinel special-casing) — rather than
    falling all the way to ``extract_json_from_response``'s ``---DRAFT---`` shortcut, which
    would discard the structured payload entirely."""
    agent = _FakeAgent(
        'Here is the JSON: {"approved": false, "reason": "uses ---DRAFT--- before publication"}'
    )
    assert _agent_call_json(agent, "p", required_keys=("approved",)) == {
        "approved": False,
        "reason": "uses ---DRAFT--- before publication",
    }


def test_multiple_fenced_blocks_selects_anchored_last_candidate_not_first() -> None:
    """When a reply contains a fenced format example followed by the real fenced answer —
    both individually valid JSON — tier 2's ``extract_json_object`` must select the last
    ``required_keys``-anchored candidate, not just parse whichever fenced block comes first
    (the bug in ``extract_json_from_response``'s own first-fenced-block fast path)."""
    agent = _FakeAgent(
        'Format example:\n```json\n{"approved": true}\n```\n'
        'Actual answer:\n```json\n{"approved": false}\n```'
    )
    assert _agent_call_json(agent, "p", required_keys=("approved",)) == {"approved": False}


def test_tier3_non_dict_result_is_rejected() -> None:
    """A fenced JSON array with surrounding prose is correctly declined by tier 2's
    ``extract_json_object`` (dict-only contract). Tier 3's ``extract_json_from_response``
    parses and returns the bare list instead of declining it — ``_agent_call_json`` must
    reject that too, since its own dict contract is what every call site relies on
    (``data.get(...)``); leaking a list would surface as an uncaught ``AttributeError`` in
    the caller instead of the safe-default/retry path a parse failure gets."""
    agent = _FakeAgent("Some prose ```json\n[1, 2, 3]\n``` more prose")
    with pytest.raises(json.JSONDecodeError):
        _agent_call_json(agent, "p")


def test_tier2_non_dict_result_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """``extract_json_object`` is documented and typed to return only ``Dict[str, Any]`` or
    ``None``, but ``_agent_call_json`` must not trust that contract blindly: if tier 2 ever
    returns a non-dict (e.g. a list), it must be rejected the same way tier 3's non-dict
    results already are, rather than leaking past the function's own dict contract."""
    import software_engineering_team.tech_lead_agent.agent as tl_mod

    monkeypatch.setattr(
        tl_mod, "extract_json_object", lambda text, required_keys=None: ["not", "a", "dict"]
    )
    with pytest.raises(json.JSONDecodeError):
        _agent_call_json(_FakeAgent("irrelevant, extract_json_object is mocked"), "p")


def test_tier3_anchor_mismatch_is_rejected() -> None:
    """A reply containing only unrelated prose-wrapped JSON (e.g. a stray usage/token
    report) is correctly declined by tier 2's anchor check. Tier 3's
    ``extract_json_from_response`` recovers and returns that anchor-less object anyway (its
    early recovery stages don't consult ``expected_keys``) — ``_agent_call_json`` must reject
    it too, so a caller like ``run_revision_adjudication`` sees a genuine parse failure (and
    gets its remaining retry attempts) instead of a "successful" call that returns an object
    with no ``verdict`` key."""
    agent = _FakeAgent('Usage report: {"tokens": 9}')
    with pytest.raises(json.JSONDecodeError):
        _agent_call_json(agent, "p", required_keys=("verdict",))
