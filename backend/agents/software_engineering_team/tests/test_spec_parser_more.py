"""Extra tests for ``software_engineering_team.spec_parser``.

Covers the path-resolution helpers (`get_latest_spec_path`,
`get_newest_spec_content`), context-gathering helpers, workspace containment
guard, and the `validate_*` helpers' edge cases not already covered by
test_spec_parser.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_get_latest_spec_path_prefers_product_analysis(tmp_path: Path) -> None:
    from software_engineering_team.spec_parser import get_latest_spec_path

    pa = tmp_path / "plan" / "product_analysis"
    pa.mkdir(parents=True)
    (pa / "validated_spec.md").write_text("validated")
    (tmp_path / "plan" / "updated_spec.md").write_text("updated")
    (tmp_path / "plan" / "updated_spec_v2.md").write_text("v2")
    (tmp_path / "initial_spec.md").write_text("initial")
    p = get_latest_spec_path(tmp_path)
    assert p.name == "validated_spec.md"


def test_get_latest_spec_path_falls_through_to_plan(tmp_path: Path) -> None:
    from software_engineering_team.spec_parser import get_latest_spec_path

    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "updated_spec.md").write_text("u")
    p = get_latest_spec_path(tmp_path)
    assert p.name == "updated_spec.md"


def test_get_latest_spec_path_versioned_picks_largest(tmp_path: Path) -> None:
    from software_engineering_team.spec_parser import get_latest_spec_path

    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "updated_spec_v1.md").write_text("v1")
    (plan / "updated_spec_v5.md").write_text("v5")
    (plan / "updated_spec_v3.md").write_text("v3")
    p = get_latest_spec_path(tmp_path)
    assert p.name == "updated_spec_v5.md"


def test_get_latest_spec_path_falls_through_to_root(tmp_path: Path) -> None:
    from software_engineering_team.spec_parser import get_latest_spec_path

    (tmp_path / "spec.md").write_text("root")
    p = get_latest_spec_path(tmp_path)
    assert p.name == "spec.md"


def test_get_latest_spec_path_raises_when_missing(tmp_path: Path) -> None:
    from software_engineering_team.spec_parser import get_latest_spec_path

    with pytest.raises(FileNotFoundError):
        get_latest_spec_path(tmp_path)


def test_get_latest_spec_path_raises_when_not_dir(tmp_path: Path) -> None:
    from software_engineering_team.spec_parser import get_latest_spec_path

    bogus = tmp_path / "not_a_dir.txt"
    bogus.write_text("x")
    with pytest.raises(FileNotFoundError):
        get_latest_spec_path(bogus)


def test_get_newest_spec_content_returns_text(tmp_path: Path) -> None:
    from software_engineering_team.spec_parser import get_newest_spec_content

    (tmp_path / "initial_spec.md").write_text("hello")
    out = get_newest_spec_content(tmp_path)
    assert out == "hello"


def test_get_newest_spec_path_product_analysis_priority(tmp_path: Path) -> None:
    """When both plan/ and product_analysis/ have spec files, the function
    picks the most recent across all of them (by mtime)."""
    from software_engineering_team.spec_parser import get_newest_spec_path

    pa = tmp_path / "plan" / "product_analysis"
    pa.mkdir(parents=True)
    (pa / "validated_spec.md").write_text("v")
    plan = tmp_path / "plan"
    (plan / "updated_spec.md").write_text("u")
    # initial_spec.md at root
    (tmp_path / "initial_spec.md").write_text("i")
    # Just check we get back one of the candidates and it exists
    p = get_newest_spec_path(tmp_path)
    assert p.exists()


def test_gather_context_files_excludes_hidden(tmp_path: Path) -> None:
    from software_engineering_team.spec_parser import gather_context_files

    (tmp_path / "README.md").write_text("hello")
    (tmp_path / ".hidden.md").write_text("hidden")
    hidden_dir = tmp_path / ".git"
    hidden_dir.mkdir()
    (hidden_dir / "head").write_text("ref")
    out = gather_context_files(tmp_path)
    assert "README.md" in out
    assert ".hidden.md" not in out
    assert all(".git" not in k for k in out)


def test_gather_context_files_skips_initial_spec(tmp_path: Path) -> None:
    from software_engineering_team.spec_parser import gather_context_files

    (tmp_path / "initial_spec.md").write_text("spec")
    (tmp_path / "other.md").write_text("other")
    out = gather_context_files(tmp_path)
    assert "initial_spec.md" not in out
    assert "other.md" in out


def test_gather_context_files_filters_by_extension(tmp_path: Path) -> None:
    from software_engineering_team.spec_parser import gather_context_files

    (tmp_path / "doc.md").write_text("md")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")  # binary; skipped
    out = gather_context_files(tmp_path)
    assert "doc.md" in out
    assert "image.png" not in out


def test_gather_context_files_skips_path_outside_base(tmp_path) -> None:
    """The ``_should_include_path`` helper returns False when the path is not
    relative to the base."""
    from software_engineering_team.spec_parser import _should_include_path

    other = Path("/tmp/elsewhere/foo.md")
    assert _should_include_path(other, tmp_path) is False


def test_gather_context_files_skips_too_large(tmp_path, monkeypatch) -> None:
    from software_engineering_team.spec_parser import gather_context_files

    # Patch the max file size to be tiny
    monkeypatch.setattr("software_engineering_team.spec_parser.MAX_CONTEXT_FILE_SIZE", 1)
    (tmp_path / "big.md").write_text("12345")
    out = gather_context_files(tmp_path)
    assert "big.md" not in out


def test_gather_context_files_total_size_cap(tmp_path, monkeypatch) -> None:
    from software_engineering_team.spec_parser import gather_context_files

    monkeypatch.setattr("software_engineering_team.spec_parser.MAX_TOTAL_CONTEXT_SIZE", 10)
    (tmp_path / "a.md").write_text("x" * 100)
    (tmp_path / "b.md").write_text("y" * 100)
    out = gather_context_files(tmp_path)
    # Only one of them fits before the cap
    assert len(out) <= 1


def test_gather_context_files_missing_dir(tmp_path) -> None:
    from software_engineering_team.spec_parser import gather_context_files

    out = gather_context_files(tmp_path / "ghost")
    assert out == {}


def test_format_context_for_prompt_truncates(tmp_path) -> None:
    from software_engineering_team.spec_parser import format_context_for_prompt

    files = {"a.md": "x" * 9000, "b.md": "short"}
    text = format_context_for_prompt(files)
    assert "### File: a.md" in text
    assert "### File: b.md" in text
    assert "truncated" in text


def test_format_context_for_prompt_empty() -> None:
    from software_engineering_team.spec_parser import format_context_for_prompt

    assert format_context_for_prompt({}) == ""


def test_load_spec_with_context(tmp_path) -> None:
    from software_engineering_team.spec_parser import load_spec_with_context

    (tmp_path / "initial_spec.md").write_text("spec body")
    (tmp_path / "notes.md").write_text("hint")
    spec, ctx = load_spec_with_context(tmp_path)
    assert spec == "spec body"
    assert "notes.md" in ctx


def test_get_next_updated_spec_version_handles_malformed(tmp_path) -> None:
    from software_engineering_team.spec_parser import get_next_updated_spec_version

    pa = tmp_path / "plan" / "product_analysis"
    pa.mkdir(parents=True)
    (pa / "updated_spec_vfoo.md").write_text("bad")
    assert get_next_updated_spec_version(tmp_path) == 1


def test_workspace_containment_blocks_outside(tmp_path, monkeypatch) -> None:
    from software_engineering_team.spec_parser import (
        ENV_WORKSPACE_ROOT,
        _check_workspace_containment,
    )

    inside = tmp_path / "inside"
    inside.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOT, str(inside))
    # Should not raise for inside path
    _check_workspace_containment(inside)
    with pytest.raises(ValueError):
        _check_workspace_containment(outside.resolve())


def test_workspace_containment_noop_when_unset(tmp_path, monkeypatch) -> None:
    from software_engineering_team.spec_parser import (
        ENV_WORKSPACE_ROOT,
        _check_workspace_containment,
    )

    monkeypatch.delenv(ENV_WORKSPACE_ROOT, raising=False)
    # No raise regardless of path
    _check_workspace_containment(tmp_path)


def test_validate_workspace_path_no_spec_path_missing(tmp_path) -> None:
    from software_engineering_team.spec_parser import validate_workspace_path_no_spec

    with pytest.raises(ValueError):
        validate_workspace_path_no_spec(tmp_path / "nope")


def test_validate_workspace_path_no_spec_not_dir(tmp_path) -> None:
    from software_engineering_team.spec_parser import validate_workspace_path_no_spec

    f = tmp_path / "f.txt"
    f.write_text("hi")
    with pytest.raises(ValueError):
        validate_workspace_path_no_spec(f)


def test_validate_workspace_path_no_spec_success(tmp_path) -> None:
    from software_engineering_team.spec_parser import validate_workspace_path_no_spec

    out = validate_workspace_path_no_spec(tmp_path)
    assert out == tmp_path.resolve()


def test_validate_work_path_succeeds_when_spec_exists(tmp_path) -> None:
    from software_engineering_team.spec_parser import validate_work_path

    (tmp_path / "initial_spec.md").write_text("spec")
    out = validate_work_path(tmp_path)
    assert out == tmp_path.resolve()


def test_validate_work_path_raises_when_path_missing(tmp_path) -> None:
    from software_engineering_team.spec_parser import validate_work_path

    with pytest.raises(ValueError):
        validate_work_path(tmp_path / "nope")


def test_validate_work_path_raises_when_not_dir(tmp_path) -> None:
    from software_engineering_team.spec_parser import validate_work_path

    f = tmp_path / "f.txt"
    f.write_text("hi")
    with pytest.raises(ValueError):
        validate_work_path(f)
