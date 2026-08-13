"""Unit tests for the agent_platform.registry loader."""

from __future__ import annotations

import threading
from pathlib import Path
from textwrap import dedent

import pytest
import yaml
from pydantic import ValidationError

from agent_platform.registry.loader import AgentRegistry, _agents_root
from agent_platform.registry.models import AgentManifest


@pytest.fixture(autouse=True)
def _no_dynamic_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these hermetic tests off the dynamic Postgres overlay.

    They construct throwaway registries and assert exact contents, so they must
    behave as the Postgres-less path regardless of whether ``POSTGRES_HOST`` is
    set in the dev environment. The overlay's own behavior is covered in
    ``test_loader_dynamic.py`` (fake store) and ``test_dynamic_store.py`` (live PG).
    """
    monkeypatch.setattr(AgentRegistry, "_dynamic_store", lambda self: None)


def _write_manifest(root: Path, team: str, filename: str, body: str) -> Path:
    directory = root / team / "agent_console" / "manifests"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(dedent(body).lstrip(), encoding="utf-8")
    return path


def test_agents_root_is_the_agents_package_directory() -> None:
    """Disk discovery must start at ``backend/agents/``, not this package's parent.

    The registry is nested under ``agent_platform/``, so a naive
    ``Path(__file__).parent.parent`` would land on ``agent_platform/`` and miss
    every team's ``agent_console/manifests/``.
    """
    root = _agents_root()
    assert root.name == "agents"
    assert (root / "blogging").is_dir()
    assert (root / "llm_service").is_dir()


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


def _manifest(agent_id: str, name: str) -> AgentManifest:
    from agent_platform.registry.models import SourceInfo

    return AgentManifest(
        id=agent_id,
        team="generated_team",
        name=name,
        summary="s",
        source=SourceInfo(entrypoint="m:f"),
    )


def test_agent_state_spec_is_exported_from_package_root() -> None:
    # AgentManifest.states is list[AgentStateSpec], so the spec must be reachable
    # via the package-root import style like the other registry spec models.
    from agent_platform import registry

    assert "AgentStateSpec" in registry.__all__
    spec = registry.AgentStateSpec
    assert spec is AgentManifest.model_fields["states"].annotation.__args__[0]


def test_manifest_states_field_is_additive_and_backward_compatible() -> None:
    # `states` is an optional additive field: a manifest validates with it omitted
    # (defaults to []) and with a populated list — old YAML keeps loading.
    from agent_platform.registry.models import AgentStateSpec

    legacy = _manifest("gen.legacy", "Legacy")
    assert legacy.states == []

    with_states = AgentManifest.model_validate(
        {
            **legacy.model_dump(),
            "states": [
                {"key": "planning", "label": "Planning", "system_prompt": "plan"},
                {"key": "executing", "label": "Executing", "system_prompt": "exec"},
            ],
        }
    )
    assert [s.key for s in with_states.states] == ["planning", "executing"]
    assert isinstance(with_states.states[0], AgentStateSpec)


def test_register_installs_and_overwrites() -> None:
    reg = AgentRegistry([], {})
    first = _manifest("gen.a", "First")
    reg.register(first)
    assert reg.get("gen.a") is first

    # Re-registering the same id overwrites the prior entry (idempotent install).
    second = _manifest("gen.a", "Second")
    reg.register(second)
    assert reg.get("gen.a") is second
    assert reg.get("gen.a").name == "Second"


def test_concurrent_register_unregister_and_iteration_do_not_race() -> None:
    # register()/unregister() mutate _by_id's size; manifests_with_id_prefix()'s
    # `[m for m in self._by_id.values() if ...]` iterates it with per-item Python
    # bytecode (the filter condition), which is the shape CPython can actually
    # interrupt mid-iteration. Without AgentRegistry's internal lock this reliably
    # raises "RuntimeError: dictionary changed size during iteration" under load
    # (confirmed by temporarily stubbing out the lock) — a large seeded registry
    # gives the iteration enough width for a concurrent register()/unregister() to
    # land mid-scan.
    seed = [_manifest(f"seed.{i}", "N") for i in range(3000)]
    reg = AgentRegistry(seed, {})
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer(i: int) -> None:
        for n in range(500):
            try:
                reg.register(_manifest(f"gen.race-{i}-{n}", "N"))
                reg.unregister(f"gen.race-{i}-{n}")
            except BaseException as exc:  # noqa: BLE001 - capture to report from the main thread
                errors.append(exc)

    def reader() -> None:
        while not stop.is_set():
            try:
                reg.manifests_with_id_prefix("gen.")
                reg.all()
                reg.search()
                reg.teams()
            except BaseException as exc:  # noqa: BLE001 - capture to report from the main thread
                errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    writers = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    for t in readers:
        t.join()

    assert errors == []


def test_manifests_with_id_prefix_returns_only_matching() -> None:
    reg = AgentRegistry([], {})
    reg.register(_manifest("team-a.one", "A1"))
    reg.register(_manifest("team-a.two", "A2"))
    reg.register(_manifest("team-b.one", "B1"))

    matched = reg.manifests_with_id_prefix("team-a.")
    assert {m.id for m in matched} == {"team-a.one", "team-a.two"}
    assert reg.manifests_with_id_prefix("nope.") == []


def test_manifests_with_id_prefix_empty_registry_returns_empty() -> None:
    reg = AgentRegistry([], {})
    assert reg.manifests_with_id_prefix("anything.") == []


def test_manifests_with_id_prefix_excludes_tombstoned_id_with_no_active_store() -> None:
    # No dynamic store active (the _no_dynamic_store autouse fixture): the
    # local-only fallback branch must still exclude a recently unregistered id.
    reg = AgentRegistry([], {})
    reg.register(_manifest("team-a.tombstoned", "A"))
    reg.unregister("team-a.tombstoned")
    assert reg.manifests_with_id_prefix("team-a.") == []


def test_manifests_with_id_prefix_empty_prefix_matches_all() -> None:
    # Every string startswith("") — an empty prefix returns the whole registry.
    reg = AgentRegistry([], {})
    reg.register(_manifest("team-a.one", "A1"))
    reg.register(_manifest("team-b.one", "B1"))
    assert {m.id for m in reg.manifests_with_id_prefix("")} == {"team-a.one", "team-b.one"}


def test_register_tracks_source_path(tmp_path: Path) -> None:
    reg = AgentRegistry([], {})
    reg.register(_manifest("gen.b", "B"), source_path=tmp_path / "b.yaml")
    assert reg._source_paths["gen.b"] == tmp_path / "b.yaml"


def test_unregister_removes_and_reports(tmp_path: Path) -> None:
    reg = AgentRegistry([], {})
    reg.register(_manifest("gen.c", "C"), source_path=tmp_path / "c.yaml")

    assert reg.unregister("gen.c") is True
    assert reg.get("gen.c") is None
    assert "gen.c" not in reg._source_paths
    # Removing an unknown id is a no-op that reports False.
    assert reg.unregister("gen.c") is False
    assert reg.unregister("never.registered") is False


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
        cognition:
          tools: [git]
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
    assert summary.has_cognition is True
    # A cognition block with no explicit `knowledge_graph` is graph-enabled by default.
    assert summary.has_knowledge_graph is True


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


def test_cognition_spec_round_trip_with_block(tmp_path: Path) -> None:
    """A full `cognition:` block parses and every field round-trips."""
    _write_manifest(
        tmp_path,
        "blogging",
        "smart.yaml",
        """
        schema_version: 1
        id: blogging.smart
        team: blogging
        name: Smart
        summary: has a cognition block
        cognition:
          memory:
            retention_days_events: 30
          tools: [git, http_api]
          rule_packs: [default_guardrails]
          requires_idempotency_key: true
        source:
          entrypoint: x:y
        """,
    )
    reg = AgentRegistry.load(tmp_path)
    smart = reg.get("blogging.smart")
    assert smart is not None
    assert smart.cognition is not None
    assert smart.cognition.memory.retention_days_events == 30
    assert smart.cognition.tools == ["git", "http_api"]
    assert smart.cognition.rule_packs == ["default_guardrails"]
    assert smart.cognition.requires_idempotency_key is True


def test_cognition_spec_absent_defaults_to_none(tmp_path: Path) -> None:
    """Manifests without a `cognition:` block still load; the field is None."""
    _write_manifest(
        tmp_path,
        "blogging",
        "plain.yaml",
        """
        schema_version: 1
        id: blogging.plain
        team: blogging
        name: Plain
        summary: no cognition block
        source:
          entrypoint: x:y
        """,
    )
    reg = AgentRegistry.load(tmp_path)
    plain = reg.get("blogging.plain")
    assert plain is not None
    assert plain.cognition is None
    [summary] = reg.search()
    assert summary.has_cognition is False
    assert summary.has_knowledge_graph is False


def test_cognition_spec_partial_block_uses_defaults(tmp_path: Path) -> None:
    """A `cognition:` block that omits sub-fields fills in safe defaults."""
    _write_manifest(
        tmp_path,
        "blogging",
        "partial.yaml",
        """
        schema_version: 1
        id: blogging.partial
        team: blogging
        name: Partial
        summary: cognition block with only one field set
        cognition:
          tools: [git]
        source:
          entrypoint: x:y
        """,
    )
    reg = AgentRegistry.load(tmp_path)
    partial = reg.get("blogging.partial")
    assert partial is not None
    assert partial.cognition is not None
    assert partial.cognition.tools == ["git"]
    # Omitted sub-fields fall back to defaults, never missing attributes.
    assert partial.cognition.memory.retention_days_events == 90
    assert partial.cognition.rule_packs == []
    assert partial.cognition.requires_idempotency_key is False
    # An omitted `knowledge_graph` block attaches a default-on graph.
    assert partial.cognition.knowledge_graph.enabled is True
    assert partial.cognition.knowledge_graph.ingest_events is True
    assert partial.cognition.knowledge_graph.ingest_summaries is True
    assert partial.cognition.knowledge_graph.ground_rule_proposals is True
    [summary] = reg.search()
    assert summary.has_knowledge_graph is True


def test_cognition_knowledge_graph_opt_out(tmp_path: Path) -> None:
    """`knowledge_graph.enabled: false` opts the agent out; the summary flag is False."""
    _write_manifest(
        tmp_path,
        "blogging",
        "optout.yaml",
        """
        schema_version: 1
        id: blogging.optout
        team: blogging
        name: Opt Out
        summary: cognition present but graph disabled
        cognition:
          knowledge_graph:
            enabled: false
        source:
          entrypoint: x:y
        """,
    )
    reg = AgentRegistry.load(tmp_path)
    optout = reg.get("blogging.optout")
    assert optout is not None
    assert optout.cognition is not None
    assert optout.cognition.knowledge_graph.enabled is False
    [summary] = reg.search()
    assert summary.has_cognition is True
    assert summary.has_knowledge_graph is False


def test_cognition_retention_must_be_positive(tmp_path: Path) -> None:
    """`retention_days_events` < 1 violates the precondition and fails validation."""
    raw = {
        "schema_version": 1,
        "id": "blogging.bad",
        "team": "blogging",
        "name": "Bad",
        "summary": "non-positive retention",
        "cognition": {"memory": {"retention_days_events": 0}},
        "source": {"entrypoint": "x:y"},
    }
    with pytest.raises(ValidationError):
        AgentManifest.model_validate(raw)

    # The loader swallows the ValidationError and skips the manifest entirely.
    _write_manifest(tmp_path, "blogging", "bad.yaml", yaml.safe_dump(raw))
    reg = AgentRegistry.load(tmp_path)
    assert reg.get("blogging.bad") is None


def test_cognition_example_manifest_is_valid() -> None:
    """The shipped standalone example manifest parses as a valid AgentManifest."""
    example = (
        Path(__file__).resolve().parents[3]
        / "agent_cognition"
        / "examples"
        / "cognition_manifest.example.yaml"
    )
    manifest = AgentManifest.model_validate(yaml.safe_load(example.read_text(encoding="utf-8")))
    assert manifest.cognition is not None
    assert manifest.cognition.tools == ["git", "http_api"]
    assert manifest.cognition.rule_packs == ["default_guardrails"]
    assert manifest.cognition.memory.retention_days_events == 90
    assert manifest.cognition.requires_idempotency_key is False
    assert manifest.cognition.knowledge_graph.enabled is True
    assert manifest.cognition.knowledge_graph.ingest_events is True
    assert manifest.cognition.knowledge_graph.ingest_summaries is True
    assert manifest.cognition.knowledge_graph.ground_rule_proposals is True


def test_tombstone_ttl_defaults_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = AgentRegistry([], {})
    assert reg._TOMBSTONE_TTL_S == AgentRegistry._DEFAULT_TOMBSTONE_TTL_S

    monkeypatch.setenv("AGENT_REGISTRY_TOMBSTONE_TTL_S", "12.5")
    assert reg._TOMBSTONE_TTL_S == 12.5

    # Negative values clamp to 0.0 rather than producing a negative TTL.
    monkeypatch.setenv("AGENT_REGISTRY_TOMBSTONE_TTL_S", "-3")
    assert reg._TOMBSTONE_TTL_S == 0.0

    # Unparseable values fall back to the default rather than raising.
    monkeypatch.setenv("AGENT_REGISTRY_TOMBSTONE_TTL_S", "not-a-number")
    assert reg._TOMBSTONE_TTL_S == AgentRegistry._DEFAULT_TOMBSTONE_TTL_S


def test_tombstone_max_entries_defaults_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = AgentRegistry([], {})
    assert reg._TOMBSTONE_MAX_ENTRIES == AgentRegistry._DEFAULT_TOMBSTONE_MAX_ENTRIES

    monkeypatch.setenv("AGENT_REGISTRY_TOMBSTONE_MAX_ENTRIES", "50")
    assert reg._TOMBSTONE_MAX_ENTRIES == 50

    # Zero/negative values clamp to 1 rather than a non-positive cap.
    monkeypatch.setenv("AGENT_REGISTRY_TOMBSTONE_MAX_ENTRIES", "0")
    assert reg._TOMBSTONE_MAX_ENTRIES == 1

    # Unparseable values fall back to the default rather than raising.
    monkeypatch.setenv("AGENT_REGISTRY_TOMBSTONE_MAX_ENTRIES", "not-a-number")
    assert reg._TOMBSTONE_MAX_ENTRIES == AgentRegistry._DEFAULT_TOMBSTONE_MAX_ENTRIES
