"""Unit tests for the shared filesystem path-safety guard."""

from __future__ import annotations

import pytest

from agent_provisioning_team.shared.path_safety import safe_path_component


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
        "a..b",  # embedded double-dot
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
