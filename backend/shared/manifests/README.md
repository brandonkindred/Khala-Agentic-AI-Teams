# shared.manifests

Single platform API for constructing generated and Studio-authored
`AgentManifest` instances. Sibling to `shared.postgres` / `shared.temporal`:
authoring surfaces do not assemble manifests ad hoc — they call these helpers
and pass the generated-agent constants defined here.

Nothing in this package performs I/O. Helpers are pure; constants are dotted
refs and a cognition factory. Persistence still belongs to
`agent_platform.registry` (`AgentRegistry.register` / the dynamic store).

## Why

Studio (`agent_platform.studio.registration`) and agentic provisioning
(`agent_team_studio.agentic_team_provisioning.manifest_generation`) build the
same overall `AgentManifest` shape: pick inline-vs-ref I/O, stamp source +
cognition, then round-trip through `revalidate`. This package is that shared
tail. Surfaces keep what is actually surface-specific (ids, team keys,
skill-tag wrapping, state-fold / refine-draft rules, registry writes).

## Public API

Import from `shared.manifests` (the package `__all__`), not from
`shared.manifests.builders` / `shared.manifests.constants` at call sites.

| Symbol | Kind | Role |
|---|---|---|
| `build_manifest(...)` | helper | Assemble fields into a validated `AgentManifest` via `revalidate` |
| `clone_manifest(manifest, **overrides)` | helper | Copy + override fields; never mutates the source |
| `io_schema(inline, *, schema_ref, ...)` | helper | Authored inline JSON Schema when present; otherwise a dotted `schema_ref` |
| `project_manifest(manifest, *, strip_tags=...)` | helper | Author-facing dict (`name`, `summary`, `tags`, `tools`, schemas, `states`) |
| `GENERATED_AGENT_ENTRYPOINT` | constant | Shared sandbox callable for generated/authored agents |
| `GENERATED_AGENT_INPUT_REF` | constant | Dotted ref for the invoke input model |
| `GENERATED_AGENT_OUTPUT_REF` | constant | Dotted ref for the invoke output model |
| `AGENT_ANATOMY_REF` | constant | Repo-relative anatomy contract path |
| `DEFAULT_RULE_PACKS` | constant | Day-one cognition seed pack names |
| `default_cognition_block()` | factory | 90-day memory, empty `tools`, `default_guardrails`, default-on knowledge graph |

`GENERATED_AGENT_ENTRYPOINT`, `GENERATED_AGENT_INPUT_REF`, and
`GENERATED_AGENT_OUTPUT_REF` are assigned only in
[`constants.py`](constants.py). Callers import them from this package.

## Ownership

| Concern | Owner |
|---|---|
| Build/clone/project helpers and generated-agent refs | `shared.manifests` |
| Hashing primitives (`slug`, `hash_suffix`) and `revalidate` | `agent_platform.registry.manifest_projection` |
| Studio ids, 3-state fold, refine-draft rules | `agent_platform.studio.registration` |
| Roster ids, skill-tag wrapping, `register_team_manifests` | `agent_team_studio.agentic_team_provisioning.manifest_generation` |
| Team keys (`agent_studio`, `agentic_team_provisioning`) | each surface (must match `TEAM_CONFIGS`) |
| Catalog persistence | `agent_platform.registry` |

`agent_team_studio.manifest_shared` may re-export anatomy / rule-pack /
cognition names as thin shims. It does **not** own or re-export the generated
entrypoint or invoke-schema refs.

## Callers

```python
from shared.manifests import (
    AGENT_ANATOMY_REF,
    GENERATED_AGENT_ENTRYPOINT,
    GENERATED_AGENT_INPUT_REF,
    GENERATED_AGENT_OUTPUT_REF,
    build_manifest,
    default_cognition_block,
    io_schema,
)

source = SourceInfo(entrypoint=GENERATED_AGENT_ENTRYPOINT, anatomy_ref=AGENT_ANATOMY_REF)
cognition = default_cognition_block().model_copy(update={"tools": tool_ids})
inputs = io_schema(inline_or_none, schema_ref=GENERATED_AGENT_INPUT_REF, ...)
manifest = build_manifest(id=..., team=..., name=..., summary=..., source=source, cognition=cognition, inputs=inputs, ...)
```

Field mapping for Studio's `AgentDefinition` view-model:
[`agent_platform/studio/README.md`](../../agents/agent_platform/studio/README.md).
Roster Manifest SoT:
[`agentic_team_provisioning/README.md`](../../agents/agent_team_studio/agentic_team_provisioning/README.md).

## Out of scope

Runtime persona binding — a saved agent's `role` / `system_prompt` are
advertised on the manifest, but the shared generated-agent runtime still
reconstructs persona from the invoke request body at invoke time. Binding
that advertised persona at runtime is a separate follow-up.
