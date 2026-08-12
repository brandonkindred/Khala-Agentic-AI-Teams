# Host persona audit under Agent Studio

**Date:** 2026-08-12  
**Status:** Approved for implementation planning  
**Scope:** Angular Agent Studio frontend only (`user-interface/src/app`)

## Problem

Agent Studio stages 1–4 already mount catalog, runner, compose, and persona
children, so the build → test → compose → persona-test journey can run inside
`/agent-studio`. The persona **audit** view is the remaining functional
dependency on the old Testing Personas surface: it is routed only at
`/persona-testing/audit/:runId`, and its back link goes to `/persona-testing`.

Stage 4 already shows an inline live-run (elapsed time, thinking indicator,
decision transcript). Users still cannot open the full audit (overview tabs,
artifacts, persona chat) without leaving Studio for the old route.

## Goal

Studio is a complete host for the persona-test audit:

- Nested route `/agent-studio/persona-run/:runId` renders the existing audit
  panel inside the Studio shell.
- Stage 4 keeps its inline live-run and adds a **View full audit** control that
  opens that nested route.
- Back from audit returns to `/agent-studio` on Stage 4 (Personas).
- No Studio path navigates to `/persona-testing` or `/persona-testing/audit/:runId`.

## Non-goals

- Do **not** delete `/persona-testing`, `/persona-testing/audit/:runId`,
  `/agent-console`, or `/agentic-teams` (separate cutover).
- Do **not** add in-context Browse agents / Test ▸ slide-outs (separate work).
- Do **not** replace Stage 4’s inline live-run with the audit panel.
- Do **not** change audit polling, tabs, artifacts, or persona-chat behavior.
- Do **not** change backend APIs.

## Decisions (locked)

| Decision | Choice |
|---|---|
| How Stage 4 opens audit | Keep inline live-run; **View full audit** navigates to the nested Studio route |
| Routing | Child of `/agent-studio`, not a sibling that unmounts the shell |
| Shell chrome on audit | Header + stepper stay; continue footer is hidden |
| Session state | `AgentStudioStateService` / `AgentStudioFacade` stay provided on the shell so they survive the round-trip |
| Back control | Parameterize the panel’s existing back link; wrapper sets Stage 4 on init so a `routerLink` to `/agent-studio` lands on Personas |
| Old audit route | Unchanged, including default back link to `/persona-testing` |
| Deep link to audit | Wrapper calls `navigateToStage(4)` on init so the stepper shows Personas |

## Architecture

`/agent-studio` remains `AgentStudioShellComponent`. The shell keeps the header
and forward-only stepper. Its main area becomes a `router-outlet` with two
children:

| Path | Component | Route `data` |
|---|---|---|
| `''` | `AgentStudioStageHostComponent` | (none) |
| `persona-run/:runId` | `AgentStudioPersonaAuditComponent` | `{ hideStudioFooter: true }` |

```
/agent-studio
  └─ AgentStudioShellComponent
       header + stepper
       <router-outlet>
         ''                  → stage host (Build / Test / Compose / Personas)
         persona-run/:runId  → Studio audit wrapper → existing audit panel
       footer (hidden when child data.hideStudioFooter)
```

Because the shell stays mounted, handoff state (`registryAgentId`, `teamId`,
`processId`, `personaId`, active stage) is unchanged after navigating to audit
and back.

The continue footer stays on the shell (it already owns `forwardDisabled` /
`onContinue`) and is omitted when the active child route has
`hideStudioFooter: true`.

`/persona-testing/audit/:runId` continues to load `PersonaTestAuditPanelComponent`
directly.

## Components

### `AgentStudioStageHostComponent` (new)

Extract the shell’s current `@switch` of Build / Test / Compose / Personas
(and the unused placeholder default) into this default child so the outlet has
a real empty-path component. No behavior change.

### `AgentStudioPersonaAuditComponent` (new)

Thin Studio layout wrapper:

- On init: `navigateToStage(4)` (Personas).
- Template: mounts `app-persona-test-audit-panel` with Studio back inputs
  (below). Does not reimplement polling, tabs, or artifacts, and does not
  read `:runId` — the panel still takes it from `ActivatedRoute` (the
  wrapper is the routed component, so the panel sees the same `persona-run/:runId`
  params).
- Fills the Studio main area (block host, height 100%).

### `PersonaTestAuditPanelComponent` (existing)

Add optional inputs with defaults that preserve the old dashboard:

- `backLink` — default `'/persona-testing'`
- `backLabel` — default `'Back to Testing Personas'`

The existing back `<a routerLink>` binds these inputs. The Studio wrapper
passes `backLink="/agent-studio"` and `backLabel="Back to Agent Studio"`.

No second Back control on the wrapper. Stage 4 is already selected on wrapper
init, so navigating to `/agent-studio` restores Personas without a click
handler.

### Stage 4 (`AgentStudioPersonaComponent`)

Keep the inline live-run. When `run()` is non-null (live or terminal), show
**View full audit** in the live-run header. Clicking it navigates to
`/agent-studio/persona-run/:runId` using `run().run_id`. It never navigates to
`/persona-testing`.

The control is absent when there is no current run.

## Error handling

Reuse the audit panel’s existing surfaces:

- Missing `:runId` → “No run ID provided”
- Failed status fetch → panel error string (unchanged)

Stage 4 does not render **View full audit** without a run, so it never
navigates without an id.

Back from a deep-linked audit can land on Stage 4 with no `teamId`. Stage 4
already shows “Compose a team in Stage 3” — no new banner.

The old `/persona-testing/audit/:runId` error paths are unchanged.

## Testing

Frontend unit tests only. New and changed files must meet the 90% line-coverage
floor.

- **Stage 4:** **View full audit** is absent with no run; with a run it is
  present and navigates to `/agent-studio/persona-run/:runId` — never
  `/persona-testing`.
- **Audit wrapper:** on init, active stage is Personas; it mounts the audit
  panel with Studio `backLink` / `backLabel`; a missing `:runId` still reaches
  the panel (panel owns that error).
- **Audit panel:** default back link stays `/persona-testing` with the current
  label; custom `backLink` / `backLabel` render when provided. Existing
  status / error tests stay.
- **Shell:** default child shows the stage host and footer; on
  `persona-run/:runId` the footer is hidden and the wrapper renders. Handoff
  state is unchanged after navigating to audit and back.

## Out of scope (explicit)

- Deleting old routes, nav items, or dashboard shells
- In-context Browse / Test ▸ actions from later stages
- Relocating jobs-dashboard deep links that still point at `/persona-testing`
- Stage 1.2 / 1.3 build-assistant placeholders
