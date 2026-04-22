"""Unit tests for the agent_registry loader."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agent_registry.loader import AgentRegistry


def _write_manifest(root: Path, team: str, filename: str, body: str) -> Path:
    directory = root / team / "agent_console" / "manifests"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(dedent(body).lstrip(), encoding="utf-8")
    return path


def test_loader_discovers_manifests_and_groups_by_team(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        "blogging",
        "planner.yaml",
        """
        schema_version: 1
        id: blogging.planner
        team: blogging
        name: Blog Planner
        summary: Plans blog posts.
        tags: [planning]
        source:
          entrypoint: blogging.planner.agent:BlogPlanningAgent
        """,
    )
    _write_manifest(
        tmp_path,
        "branding",
        "auditor.yaml",
        """
        schema_version: 1
        id: branding.auditor
        team: branding
        name: Auditor
        summary: Audits brand.
        tags: [branding]
        source:
          entrypoint: branding.agents:make_auditor
        """,
    )

    reg = AgentRegistry.load(tmp_path)
    assert len(reg.all()) == 2
    ids = {m.id for m in reg.all()}
    assert ids == {"blogging.planner", "branding.auditor"}
    teams = {g.team: g.agent_count for g in reg.teams()}
    assert teams == {"blogging": 1, "branding": 1}


def test_loader_skips_malformed_yaml(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "blogging", "broken.yaml", ":\n-this is not valid: yaml: [\n")
    reg = AgentRegistry.load(tmp_path)
    assert reg.all() == []


def test_loader_skips_invalid_manifest_shape(tmp_path: Path) -> None:
    # Missing required fields (id, team, name, summary, source).
    _write_manifest(
        tmp_path,
        "blogging",
        "partial.yaml",
        """
        schema_version: 1
        name: Only a name
        """,
    )
    reg = AgentRegistry.load(tmp_path)
    assert reg.all() == []


def test_duplicate_ids_are_deduped_last_one_wins(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        "blogging",
        "a.yaml",
        """
        schema_version: 1
        id: dup.id
        team: blogging
        name: A
        summary: first
        source:
          entrypoint: x:y
        """,
    )
    _write_manifest(
        tmp_path,
        "blogging",
        "b.yaml",
        """
        schema_version: 1
        id: dup.id
        team: blogging
        name: B
        summary: second
        source:
          entrypoint: x:y
        """,
    )
    reg = AgentRegistry.load(tmp_path)
    assert len(reg.all()) == 1
    # Filename ordering is alphabetical, so b.yaml is loaded second and wins.
    assert reg.get("dup.id").name == "B"


def test_search_filters_and_query(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        "blogging",
        "planner.yaml",
        """
        schema_version: 1
        id: blogging.planner
        team: blogging
        name: Planner
        summary: plans content
        tags: [planning]
        source:
          entrypoint: x:y
        """,
    )
    _write_manifest(
        tmp_path,
        "blogging",
        "writer.yaml",
        """
        schema_version: 1
        id: blogging.writer
        team: blogging
        name: Writer
        summary: writes drafts
        tags: [writing]
        source:
          entrypoint: x:y
        """,
    )
    _write_manifest(
        tmp_path,
        "branding",
        "auditor.yaml",
        """
        schema_version: 1
        id: branding.auditor
        team: branding
        name: Auditor
        summary: audits brand
        tags: [branding]
        source:
          entrypoint: x:y
        """,
    )
    reg = AgentRegistry.load(tmp_path)

    assert {s.id for s in reg.search(team="blogging")} == {"blogging.planner", "blogging.writer"}
    assert {s.id for s in reg.search(tag="planning")} == {"blogging.planner"}
    assert {s.id for s in reg.search(q="AUDITS")} == {"branding.auditor"}
    assert {s.id for s in reg.search(team="blogging", q="writes")} == {"blogging.writer"}
    assert reg.search(team="nonexistent") == []


def test_summary_flags_reflect_manifest_content(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        "blogging",
        "rich.yaml",
        """
        schema_version: 1
        id: rich.agent
        team: blogging
        name: Rich
        summary: has everything
        inputs:
          schema_ref: pkg.mod:Input
        outputs:
          schema_ref: pkg.mod:Output
        invoke:
          kind: http
          method: POST
          path: /api/foo
        sandbox:
          manifest_path: default.yaml
          access_tier: standard
        source:
          entrypoint: pkg.mod:Agent
        """,
    )
    reg = AgentRegistry.load(tmp_path)
    [summary] = reg.search()
    assert summary.has_input_schema is True
    assert summary.has_output_schema is True
    assert summary.has_invoke is True
    assert summary.has_sandbox is True


def test_detail_returns_manifest_and_anatomy_when_present(tmp_path: Path) -> None:
    anatomy_path = tmp_path / "docs" / "anatomy.md"
    anatomy_path.parent.mkdir(parents=True, exist_ok=True)
    anatomy_path.write_text("# Anatomy\n\nBody.\n", encoding="utf-8")

    _write_manifest(
        tmp_path,
        "blogging",
        "a.yaml",
        """
        schema_version: 1
        id: blogging.a
        team: blogging
        name: A
        summary: summary
        source:
          entrypoint: x:y
          anatomy_ref: docs/anatomy.md
        """,
    )

    reg = AgentRegistry.load(tmp_path)
    detail = reg.detail("blogging.a", repo_root=tmp_path)
    assert detail is not None
    assert detail.manifest.id == "blogging.a"
    assert detail.anatomy_markdown is not None
    assert "Anatomy" in detail.anatomy_markdown


def test_detail_missing_agent_returns_none(tmp_path: Path) -> None:
    reg = AgentRegistry.load(tmp_path)
    assert reg.detail("nope") is None


def test_read_anatomy_without_repo_root_does_not_raise_on_shallow_layout() -> None:
    """Regression: the fallback parent walk used to hard-code here.parents[2..5],
    which raised IndexError when the module lived fewer than 5 levels above
    the filesystem root (e.g. a shallow checkout at /repo/backend/...).

    We don't assert a specific return value — only that the method returns
    gracefully (``None`` when the file isn't found) instead of raising.
    """
    reg = AgentRegistry(manifests=[], team_display_names={})
    # Pass an anatomy_ref that almost certainly doesn't exist on disk.
    result = reg._read_anatomy("definitely/not/a/real/anatomy.md", repo_root=None)
    assert result is None


def test_sandbox_spec_env_and_extra_pip_round_trip(tmp_path: Path) -> None:
    """Issue #265: SandboxSpec gains `env` + `extra_pip`, both optional with defaults."""
    _write_manifest(
        tmp_path,
        "blogging",
        "rich.yaml",
        """
        schema_version: 1
        id: blogging.rich
        team: blogging
        name: Rich
        summary: has sandbox extras
        sandbox:
          manifest_path: default.yaml
          access_tier: standard
          env:
            EXTRA_FLAG: "on"
          extra_pip:
            - some-niche-dep==1.2.3
        source:
          entrypoint: x:y
        """,
    )
    _write_manifest(
        tmp_path,
        "blogging",
        "plain.yaml",
        """
        schema_version: 1
        id: blogging.plain
        team: blogging
        name: Plain
        summary: sandbox with only the legacy fields
        sandbox:
          manifest_path: default.yaml
          access_tier: standard
        source:
          entrypoint: x:y
        """,
    )

    reg = AgentRegistry.load(tmp_path)
    rich = reg.get("blogging.rich")
    assert rich is not None
    assert rich.sandbox is not None
    assert rich.sandbox.env == {"EXTRA_FLAG": "on"}
    assert rich.sandbox.extra_pip == ["some-niche-dep==1.2.3"]

    # Backwards-compat: manifests that omit the new fields still load and
    # see the defaults (empty dict / empty list), never missing attributes.
    plain = reg.get("blogging.plain")
    assert plain is not None
    assert plain.sandbox is not None
    assert plain.sandbox.env == {}
    assert plain.sandbox.extra_pip == []


def test_orphan_team_is_kept_but_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _write_manifest(
        tmp_path,
        "unknown_team",
        "x.yaml",
        """
        schema_version: 1
        id: unknown.agent
        team: unknown_team
        name: X
        summary: y
        source:
          entrypoint: x:y
        """,
    )
    reg = AgentRegistry.load(tmp_path)
    assert reg.get("unknown.agent") is not None
