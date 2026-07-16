"""Unit tests for the shared filesystem path-safety guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_provisioning_team.shared.path_safety import candidate_paths, safe_path_component


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


def test_candidate_paths_builds_primary_then_legacy() -> None:
    primary = Path("/store")
    legacy = [Path("/legacy1"), Path("/legacy2")]
    paths = candidate_paths("blog.writer", primary, legacy, ".json")
    assert paths == [
        Path("/store/blog.writer.json"),
        Path("/legacy1/blog.writer.json"),
        Path("/legacy2/blog.writer.json"),
    ]


def test_candidate_paths_appends_extension_verbatim() -> None:
    paths = candidate_paths("a1", Path("/store"), [], ".enc")
    assert paths == [Path("/store/a1.enc")]


@pytest.mark.parametrize("bad_id", ["../../etc/passwd", "a/b", "..", ".", ""])
def test_candidate_paths_rejects_unsafe_id_before_building_any_path(bad_id: str) -> None:
    # The guard runs before the primary or any legacy path is constructed, so a
    # traversal id can never reach a legacy directory regardless of ordering.
    with pytest.raises(ValueError):
        candidate_paths(bad_id, Path("/store"), [Path("/legacy")], ".json")
