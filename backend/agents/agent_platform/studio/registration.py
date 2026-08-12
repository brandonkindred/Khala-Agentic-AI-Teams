"""Clone-from-registry and save+register for authored Studio agents.

Saving reuses the **generated-agent runtime**: a saved Studio agent is registered
into the process-wide ``agent_registry`` with the same entrypoint and invoke
schemas that ``agentic_team_provisioning`` stamps onto generated team agents, so
it resolves via ``GET /api/agents/{id}`` and runs via ``POST /api/agents/{id}/invoke``
without a YAML file.

Runtime-binding caveat (inherited from generated agents): the generated runtime
reconstructs the persona from the *invoke request body*, not the stored manifest,
so a saved agent's persisted ``role`` / ``system_prompt`` (the latter via the
``executing`` state — see :func:`build_studio_agent_manifest`) are advertised but
not yet bound at invoke time. Binding is the same tracked follow-up generated team
agents carry; out of scope for this Stage-1 backend slice.

Registration scope tracks ``AgentRegistry.register()``'s own write-through: when a
dynamic Postgres store is active (``POSTGRES_HOST`` configured, not inside a
per-invoke sandbox), a saved Studio agent is persisted there too, so every other
worker and the per-invoke sandbox resolve it — not just this process. Local-only
(in-process) otherwise. See :func:`agent_registry.get_registry`.
"""

from __future__ import annotations

from agent_registry.manifest_projection import hash_suffix, revalidate, slug
from agent_registry.models import AgentManifest, AgentStateSpec, IOSchema, SourceInfo
from agent_team_studio.manifest_shared import (
    AGENT_ANATOMY_REF,
    GENERATED_AGENT_ENTRYPOINT,
    GENERATED_AGENT_INPUT_REF,
    GENERATED_AGENT_OUTPUT_REF,
    default_cognition_block,
    strip_marker_tags,
)

from .agent_states import EXECUTING_KEY, STATE_ORDER, normalize_agent_states
from .models import AgentDefinition, AgentState

# The registry "team" Studio agents are filed under (must match a TEAM_CONFIGS key).
STUDIO_TEAM = "agent_studio"

# The fixed Studio state keys. AgentStateSpec.key is a permissive str (the registry
# accepts arbitrary persisted data), so a manifest may carry a key outside this set;
# clone drops such keys and lets the AgentDefinition normalizer backfill the rest.
_KNOWN_STATE_KEYS = frozenset(STATE_ORDER)

# Tags that describe the registry plumbing rather than the agent itself; stripped
# when projecting a manifest back into an editable definition.
_PLUMBING_TAGS = frozenset({"studio", "generated", "agentic_team_provisioning"})


def studio_agent_id(name: str) -> str:
    """Return a stable, collision-resistant registry id for a Studio agent.

    Preconditions:
        * ``name`` is non-empty.
    Postconditions:
        * Returns ``agent_studio.<slug>-<hash8>``; equal names map to equal ids.
    """
    if not name.strip():
        raise ValueError("studio_agent_id: name must be non-empty")
    # Non-cryptographic: a short, stable suffix that disambiguates equal slugs.
    digest = hash_suffix(name, 8)
    return f"{STUDIO_TEAM}.{slug(name)}-{digest}"


def _io_schema(
    inline: dict | None,
    *,
    schema_ref: str,
    ref_description: str,
    inline_description: str,
) -> IOSchema:
    """Build an :class:`IOSchema` advertising an authored inline schema when present.

    Preconditions:
        * ``schema_ref`` is a non-empty dotted ref (the shared-entrypoint fallback).
    Postconditions:
        * Returns ``IOSchema(inline_schema=inline, ...)`` when ``inline is not None``
          (an authored schema was supplied — including the empty schema ``{}``, which
          is a valid JSON Schema meaning "accept anything"), else
          ``IOSchema(schema_ref=schema_ref, ...)``. Only an *omitted* schema (``None``)
          falls back to the ref; this matches the presence test (``inline_schema is
          not None``) used by the summary flags, the ``/schema/{input|output}`` route,
          and :func:`clone_from_manifest`, so an authored ``{}`` round-trips instead of
          being silently replaced by the generic ref.
    """
    if inline is not None:
        return IOSchema(inline_schema=inline, description=inline_description)
    return IOSchema(schema_ref=schema_ref, description=ref_description)


def _manifest_states(definition: AgentDefinition) -> list[AgentStateSpec]:
    """Project the definition's operating states, folding in the top-level prompt.

    Postconditions:
        * Returns one :class:`AgentStateSpec` per ``definition.states`` entry.
        * When ``definition.system_prompt`` is non-blank, it replaces the
          ``executing``-keyed entry's ``system_prompt`` (the top-level field is the
          assistant's quick-edit channel and wins over a stale per-state value).
        * A blank ``system_prompt`` leaves every entry's prompt untouched — it never
          overwrites a real per-state prompt with emptiness.
    """
    top_level_prompt = definition.system_prompt.strip()
    return [
        AgentStateSpec(
            key=s.key,
            label=s.label,
            system_prompt=definition.system_prompt
            if (top_level_prompt and s.key == EXECUTING_KEY)
            else s.system_prompt,
        )
        for s in definition.states
    ]


def build_studio_agent_manifest(definition: AgentDefinition) -> AgentManifest:
    """Build a validated, invokable ``agent_registry`` manifest for a definition.

    Preconditions:
        * ``definition.name`` is non-empty.
    Postconditions:
        * Returns a fully validated :class:`AgentManifest` filed under
          ``team == STUDIO_TEAM`` whose ``source.entrypoint`` is the shared
          generated-agent runtime, and whose ``cognition`` carries the
          ``default_guardrails`` seed pack.
        * When the definition carries an authored ``input_schema`` /
          ``output_schema`` (inline JSON), it is advertised verbatim on the
          manifest via ``IOSchema.inline_schema``; otherwise the generic
          shared-entrypoint ``schema_ref`` is advertised. (Invoke still runs
          through the shared generated-agent entrypoint regardless — runtime
          binding of the authored schema is the separate deferred follow-up.)
        * The definition's operating ``states`` are persisted onto ``manifest.states``
          (inert metadata — see :class:`AgentStateSpec`), with the top-level
          ``system_prompt`` folded into the ``executing`` state (see
          :func:`_manifest_states`).
    """
    if not definition.name.strip():
        raise ValueError("build_studio_agent_manifest: name must be non-empty")
    manifest = AgentManifest(
        id=studio_agent_id(definition.name),
        team=STUDIO_TEAM,
        name=definition.name,
        summary=definition.role or f"Studio agent {definition.name}",
        description=definition.description,
        tags=sorted({"studio", *definition.tags}),
        inputs=_io_schema(
            definition.input_schema,
            schema_ref=GENERATED_AGENT_INPUT_REF,
            ref_description="Roster metadata + user message (shared generated-agent entrypoint).",
            inline_description="Authored input schema.",
        ),
        outputs=_io_schema(
            definition.output_schema,
            schema_ref=GENERATED_AGENT_OUTPUT_REF,
            ref_description="The agent's response text.",
            inline_description="Authored output schema.",
        ),
        cognition=default_cognition_block().model_copy(update={"tools": list(definition.tools)}),
        states=_manifest_states(definition),
        source=SourceInfo(entrypoint=GENERATED_AGENT_ENTRYPOINT, anatomy_ref=AGENT_ANATOMY_REF),
    )
    # Round-trip so the returned object is guaranteed fully validated and serializable.
    return revalidate(manifest)


def clone_from_manifest(manifest: AgentManifest) -> AgentDefinition:
    """Project an existing registry manifest into an editable refine-mode draft.

    The source manifest is never mutated — this returns a *new* definition.

    Only the fields the manifest carries are cloned (name → ``<name>.copy``,
    ``summary`` → role, description, tags, cognition tools, operating states, and
    authored inline I/O schemas). An authored ``input_schema`` / ``output_schema``
    is transferred back from ``IOSchema.inline_schema`` when the source manifest
    carries one (the generic shared-entrypoint ``schema_ref`` is not — it describes
    the runtime envelope, not an authored contract). The top-level
    ``AgentDefinition.system_prompt`` is restored from the cloned ``executing``
    state's ``system_prompt`` (the inverse of :func:`_manifest_states`), so a
    refine draft starts with the same quick-edit prompt that was last saved.

    The operating ``states`` ARE transferred (the manifest persists them). Cloning
    a legacy manifest saved before states existed (empty ``states``), or one whose
    persisted keys fall outside the fixed Studio set, back-fills the missing default
    seeded states via the ``AgentDefinition`` normalizer, so every refine draft has
    exactly the three states.

    Postconditions:
        * ``mode == "refine"`` and ``cloned_from == manifest.id``.
        * The returned definition has exactly three operating states.
    """
    tools = list(manifest.cognition.tools) if manifest.cognition else []
    tags = strip_marker_tags(manifest.tags, _PLUMBING_TAGS)
    # Keep only canonical keys: AgentState.key is a Literal, so a manifest carrying
    # an unsupported (permissive-str) key would raise here and surface as a 500.
    # Normalize up front (not just left to the AgentDefinition field validator) so
    # the executing-state lookup below sees the backfilled default when a legacy
    # manifest carries no (or an incomplete) states list.
    states = normalize_agent_states(
        [
            AgentState(key=s.key, label=s.label, system_prompt=s.system_prompt)
            for s in manifest.states
            if s.key in _KNOWN_STATE_KEYS
        ]
    )
    # Avoid a confusing "name.copy.copy" when cloning an already-cloned name.
    # Per-team disambiguation (".copy-2", …) is the frontend's job — it knows the
    # team's existing names; the backend only avoids the doubled suffix here.
    name = manifest.name if manifest.name.endswith(".copy") else f"{manifest.name}.copy"
    input_schema = manifest.inputs.inline_schema if manifest.inputs else None
    output_schema = manifest.outputs.inline_schema if manifest.outputs else None
    system_prompt = next((s.system_prompt for s in states if s.key == EXECUTING_KEY), "")
    return AgentDefinition(
        name=name,
        role=manifest.summary,
        description=manifest.description,
        tags=tags,
        tools=tools,
        system_prompt=system_prompt,
        input_schema=input_schema,
        output_schema=output_schema,
        states=states,
        mode="refine",
        cloned_from=manifest.id,
    )
