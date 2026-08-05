"""Unit tests for the shared filesystem path-safety guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_team_studio.agent_provisioning_team.shared.path_safety import (
    candidate_paths,
    safe_path_component,
)


@pytest.mark.parametrize(
    "value",
    [
        "a",
        "agent-001",
        "blog.writer",  # real agent_ids can contain dots
        "a_b",
        "at-team-proc-slug-name",
        "A.B_c-1",
        "git",  # provisioner name
        "a..b",  # embedded double-dot is not a traversal (no separator) -> allowed
        "..foo",  # leading dots without a separator stay inside the store dir
    ],
)
def test_safe_path_component_accepts_valid_ids(value: str) -> None:
    # Returns the value byte-for-byte so filenames round-trip unchanged.
    assert safe_path_component(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "../../etc/passwd",  # classic traversal
        "a/b",  # forward slash
        "..\\..\\x",  # backslash traversal
        "/etc/passwd",  # absolute path
        "..",  # bare parent token
        ".",  # bare current-dir token
        "",  # empty
        "a b",  # whitespace
        "a\x00b",  # NUL byte
        "café",  # non-ASCII
    ],
)
def test_safe_path_component_rejects_unsafe_ids(value: str) -> None:
    with pytest.raises(ValueError):
        safe_path_component(value)


def test_safe_path_component_rejects_non_str() -> None:
    with pytest.raises(ValueError):
        safe_path_component(None)  # type: ignore[arg-type]


def test_safe_path_component_error_message_includes_kind() -> None:
    with pytest.raises(ValueError, match="agent_id"):
        safe_path_component("../x", kind="agent_id")


def test_safe_path_component_accepts_value_at_max_length() -> None:
    from agent_team_studio.agent_provisioning_team.shared.path_safety import _MAX_COMPONENT_LEN

    value = "a" * _MAX_COMPONENT_LEN
    assert safe_path_component(value) == value


def test_safe_path_component_rejects_overlong_value() -> None:
    from agent_team_studio.agent_provisioning_team.shared.path_safety import _MAX_COMPONENT_LEN

    with pytest.raises(ValueError, match="exceeds maximum length"):
        safe_path_component("a" * (_MAX_COMPONENT_LEN + 1), kind="agent_id")


def test_candidate_paths_reuses_primary_filename_in_legacy_dirs() -> None:
    # Each legacy candidate reuses the (already-validated) primary filename.
    primary = Path("/store/blog.writer.json")
    legacy = [Path("/legacy1"), Path("/legacy2")]
    paths = candidate_paths(primary, legacy)
    assert paths == [
        Path("/store/blog.writer.json"),
        Path("/legacy1/blog.writer.json"),
        Path("/legacy2/blog.writer.json"),
    ]


def test_candidate_paths_with_no_legacy_dirs_returns_just_primary() -> None:
    primary = Path("/store/a1.enc")
    assert candidate_paths(primary, []) == [primary]
