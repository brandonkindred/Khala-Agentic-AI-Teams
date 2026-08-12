"""Shared test doubles for the Agent Studio suites.

A package-level module (mirroring ``shared.postgres.testing``) so both the
team-level tests (``agent_studio/tests/``) and the route tests
(``unified_api/tests/``) use one ``FakeRegistry`` / ``seed_manifest`` instead of
duplicating them. Not imported by production code.
"""

from __future__ import annotations

from agent_registry.models import AgentManifest, CognitionSpec, IOSchema, SourceInfo


class FakeRegistry:
    """In-memory stand-in for ``agent_registry.AgentRegistry``.

    Satisfies the :class:`agent_platform.studio.service.RegistryLike` protocol. ``seed``
    pre-loads agents that already exist (clone/refine sources); ``register``
    records agents saved during the test. On an id collision, ``register`` wins
    over ``seed`` (see :meth:`get`), so a save is never shadowed by a seed.
    """

    def __init__(self) -> None:
        self.registered: dict[str, AgentManifest] = {}
        self._seed: dict[str, AgentManifest] = {}

    def seed(self, manifest: AgentManifest) -> None:
        self._seed[manifest.id] = manifest

    def get(self, agent_id: str) -> AgentManifest | None:
        # `registered` (agents saved during the test) takes precedence over
        # `_seed` (pre-existing agents) so a save is never shadowed by a seed of
        # the same id — that would otherwise mask a test registering onto an
        # existing id.
        return self.registered.get(agent_id) or self._seed.get(agent_id)

    def register(self, manifest: AgentManifest) -> None:
        self.registered[manifest.id] = manifest


def seed_manifest(
    *,
    agent_id: str = "blogging.planner",
    team: str = "blogging",
    name: str = "Planner",
    summary: str = "Plans blog outlines",
    tags: list[str] | None = None,
    tools: list[str] | None = None,
) -> AgentManifest:
    """Build a representative registry manifest for clone/refine tests."""
    return AgentManifest(
        id=agent_id,
        team=team,
        name=name,
        summary=summary,
        tags=tags if tags is not None else ["content"],
        cognition=CognitionSpec(
            rule_packs=["default_guardrails"],
            tools=tools if tools is not None else ["web.search"],
        ),
        inputs=IOSchema(schema_ref="x:In"),
        outputs=IOSchema(schema_ref="x:Out"),
        source=SourceInfo(entrypoint="x:run"),
    )
