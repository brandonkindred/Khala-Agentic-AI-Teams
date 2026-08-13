"""Shared build/clone/project helpers for constructing ``AgentManifest`` instances.

Both the Studio (``agent_platform.studio``) and agentic
(``agent_team_studio.agentic_team_provisioning``) authoring surfaces build
:class:`~agent_platform.registry.models.AgentManifest` instances with the same
overall shape: pick an inline-vs-ref I/O schema, assemble the manifest fields,
then round-trip through :func:`~agent_platform.registry.manifest_projection.revalidate`.
This module is the single source for that shape — the *helper functions*.
Generated-agent entrypoint/schema/anatomy refs and the default cognition
block live in :mod:`shared.manifests.constants`; helpers here stay
value-driven so callers pass those constants in rather than this module
hardcoding them.

Nothing here performs I/O — these are pure constants-free, side-effect-free
functions only.
"""

from __future__ import annotations

from typing import Any

from agent_platform.registry.manifest_projection import revalidate
from agent_platform.registry.models import AgentManifest, AgentStateSpec, CognitionSpec, IOSchema, SourceInfo


def io_schema(
    inline: dict[str, Any] | None,
    *,
    schema_ref: str,
    ref_description: str,
    inline_description: str,
) -> IOSchema:
    """Build an :class:`IOSchema` advertising an authored inline schema when present.

    Preconditions:
        * ``schema_ref`` is a non-empty dotted ref (the fallback advertised when
          no inline schema was authored).
    Postconditions:
        * Returns ``IOSchema(inline_schema=inline, description=inline_description)``
          when ``inline is not None`` (an authored schema was supplied — including
          the empty schema ``{}``, which is a valid JSON Schema meaning "accept
          anything"), else ``IOSchema(schema_ref=schema_ref, description=ref_description)``.
          Only an *omitted* schema (``None``) falls back to the ref, so an authored
          ``{}`` round-trips instead of being silently replaced by the generic ref.
    """
    if not schema_ref:
        raise ValueError("io_schema: schema_ref must be non-empty")
    if inline is not None:
        return IOSchema(inline_schema=inline, description=inline_description)
    return IOSchema(schema_ref=schema_ref, description=ref_description)


def build_manifest(
    *,
    id: str,
    team: str,
    name: str,
    summary: str,
    source: SourceInfo,
    description: str | None = None,
    tags: list[str] | None = None,
    cognition: CognitionSpec | None = None,
    inputs: IOSchema | None = None,
    outputs: IOSchema | None = None,
    states: list[AgentStateSpec] | None = None,
) -> AgentManifest:
    """Build a fully validated :class:`AgentManifest` from caller-supplied fields.

    This is the common tail shared by every current manifest builder
    (``build_studio_agent_manifest``, ``build_agent_manifest``): assemble the
    manifest, then round-trip it through :func:`revalidate`. It intentionally
    takes ``source``/``cognition``/``inputs``/``outputs`` as plain values rather
    than deriving them from hardcoded entrypoint/schema-ref constants — those
    constants live in :mod:`shared.manifests.constants`.

    Preconditions:
        * ``id``, ``team``, ``name``, ``summary`` are non-empty.
    Postconditions:
        * Returns a fully validated, JSON-safe :class:`AgentManifest` equal to
          one constructed with the given fields (``tags``/``states`` default to
          empty lists when omitted).
    """
    if not id:
        raise ValueError("build_manifest: id must be non-empty")
    if not team:
        raise ValueError("build_manifest: team must be non-empty")
    if not name:
        raise ValueError("build_manifest: name must be non-empty")
    if not summary:
        raise ValueError("build_manifest: summary must be non-empty")
    manifest = AgentManifest(
        id=id,
        team=team,
        name=name,
        summary=summary,
        description=description,
        tags=list(tags) if tags is not None else [],
        inputs=inputs,
        outputs=outputs,
        cognition=cognition,
        states=list(states) if states is not None else [],
        source=source,
    )
    return revalidate(manifest)


def clone_manifest(manifest: AgentManifest, **overrides: Any) -> AgentManifest:
    """Return a new, validated ``AgentManifest`` equal to ``manifest`` with ``overrides`` applied.

    The source manifest is never mutated.

    Preconditions:
        * ``manifest`` is a validated :class:`AgentManifest`.
        * Each key in ``overrides`` names a field on :class:`AgentManifest`.
    Postconditions:
        * Returns a new, fully revalidated :class:`AgentManifest` with every
          field of ``manifest`` carried over except those named in
          ``overrides``, which take the given values instead.
    """
    return revalidate(manifest.model_copy(update=overrides))


def project_manifest(
    manifest: AgentManifest,
    *,
    strip_tags: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Project ``manifest``'s author-facing fields into a plain dict.

    Generalizes the shared shape of every current manifest-to-view projection
    (``clone_from_manifest``'s manifest→draft conversion,
    ``skill_tags_from_manifest``'s tag extraction): the fields an authoring
    surface needs back out of a manifest to build its own team-specific view
    model (e.g. Studio's ``AgentDefinition``).

    Preconditions:
        * ``manifest`` is a validated :class:`AgentManifest`.
    Postconditions:
        * Returns a dict with keys ``name``, ``summary``, ``description``,
          ``tags`` (``manifest.tags`` minus any tag in ``strip_tags``, order
          preserved), ``tools`` (``manifest.cognition.tools``, or ``[]`` when
          ``cognition`` is absent), ``input_schema`` / ``output_schema``
          (``manifest.inputs.inline_schema`` / ``manifest.outputs.inline_schema``,
          or ``None`` when absent or not carrying an authored inline schema),
          and ``states`` (one ``{"key", "label", "system_prompt"}`` dict per
          entry of ``manifest.states``, in order).
        * ``manifest`` is not mutated.
    """
    return {
        "name": manifest.name,
        "summary": manifest.summary,
        "description": manifest.description,
        "tags": [t for t in manifest.tags if t not in strip_tags],
        "tools": list(manifest.cognition.tools) if manifest.cognition else [],
        "input_schema": manifest.inputs.inline_schema if manifest.inputs else None,
        "output_schema": manifest.outputs.inline_schema if manifest.outputs else None,
        "states": [{"key": s.key, "label": s.label, "system_prompt": s.system_prompt} for s in manifest.states],
    }
