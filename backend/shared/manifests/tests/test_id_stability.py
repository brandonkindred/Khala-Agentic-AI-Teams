"""Canonical Studio/agentic id formulas: stability, golden hashes, slug clashes.

These tests encode the hashing rules documented on :mod:`shared.manifests` so a
change to digest length, slug bounds, team prefix, or the NUL pair-hash
separator fails here before call sites migrate.
"""

from __future__ import annotations

from agent_platform.registry.manifest_projection import hash_suffix, slug


def _studio_id(name: str) -> str:
    return f"agent_studio.{slug(name)}-{hash_suffix(name, 8)}"


def _agentic_id(team_id: str, agent_name: str) -> str:
    prefix = f"agentic_team_provisioning.{slug(team_id, 12)}-{hash_suffix(team_id, 16)}."
    pair = hash_suffix(f"{team_id}\x00{agent_name}", 16)
    return f"{prefix}{slug(agent_name, 40)}-{pair}"


def test_studio_id_is_stable_and_matches_golden() -> None:
    a = _studio_id("My Cool Agent")
    b = _studio_id("My Cool Agent")
    assert a == b
    assert a == "agent_studio.my-cool-agent-92b140e9"


def test_studio_id_falls_back_to_agent_slug_for_all_symbol_name() -> None:
    assert _studio_id("!!!") == "agent_studio.agent-e84c538e"


def test_agentic_id_is_stable_and_matches_golden() -> None:
    a = _agentic_id("team-uuid-123", "Triage Agent")
    b = _agentic_id("team-uuid-123", "Triage Agent")
    assert a == b
    assert a == "agentic_team_provisioning.team-uuid-12-66d1957a640d8a6e.triage-agent-9d6de45e1af10e36"


def test_agentic_id_disambiguates_names_that_share_a_slug() -> None:
    id_qa = _agentic_id("t", "QA Agent")
    id_hyphen = _agentic_id("t", "qa-agent")
    assert id_qa != id_hyphen
    assert slug("QA Agent") == slug("qa-agent")


def test_agentic_id_disambiguates_names_that_agree_on_first_40_slug_chars() -> None:
    long_a = "X" * 50 + "alpha"
    long_b = "X" * 50 + "beta"
    assert slug(long_a, 40) == slug(long_b, 40)
    assert _agentic_id("t", long_a) != _agentic_id("t", long_b)


def test_agentic_id_disambiguates_team_ids_that_share_a_12_char_slug() -> None:
    team_a = "team-uuid-123"
    team_b = "team-uuid-129"
    assert slug(team_a, 12) == slug(team_b, 12)
    assert _agentic_id(team_a, "Triage Agent") != _agentic_id(team_b, "Triage Agent")


def test_agentic_pair_hash_binds_team_and_name_with_nul_separator() -> None:
    team_id = "team-uuid-123"
    agent_name = "Triage Agent"
    golden = _agentic_id(team_id, agent_name)
    hyphenated = hash_suffix(f"{team_id}-{agent_name}", 16)
    concatenated = hash_suffix(f"{team_id}{agent_name}", 16)
    assert hyphenated not in golden
    assert concatenated not in golden
    assert hash_suffix(f"{team_id}\x00{agent_name}", 16) in golden
