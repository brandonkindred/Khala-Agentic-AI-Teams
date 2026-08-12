# Agent Studio: load-conflict prompt and draft rename/delete

**Date:** 2026-08-12  
**Status:** Approved for implementation planning  
**Scope:** Agent Studio frontend only (`user-interface` Agent Studio shell, load-draft menu, and `AgentStudioStateService`). Backend draft `PATCH`/`DELETE` and the frontend API/facade methods already exist.

## Problem

The Studio header can save and load server drafts, but:

- Loading a draft while the session holds unsaved handoff edits overwrites that work with no prompt.
- The header does not show the bound draft’s name or a rename control.
- The Load dropdown has no per-row delete.

The UX spec (`docs/design/agent-studio-ux-spec.md` §2.4 and §3.5) requires an explicit save-first / discard choice on load, a pencil rename beside the loaded name, and `⋯ → Delete` on each Load-menu row.

## Goal

- Unsaved handoff edits never disappear on load unless the user chooses **Discard**.
- The user can **Save first**, **Discard**, or **Cancel** (abort the load).
- A bound draft can be renamed from the header without writing the payload.
- Any listed draft can be deleted, with confirm, without silently wiping the open journey.

## Non-goals

- Core save/load HTTP and furthest-stage hydration (already shipped).
- `localStorage` cache of unsaved edits (reload still starts a clean session).
- Persisting Stage 1/2/3 partial work (`stage1AgentDraft`, `stage2Inputs`, `stage3RosterDraft`) in the draft payload.
- Cutover or deletion of old Agent Console / team / persona surfaces.
- Changing the shared two-button `ConfirmDialogComponent` into a three-action dialog (load-conflict gets its own Studio dialog).
- Backend route or store changes.

## Decisions (locked)

| Decision | Choice |
|---|---|
| What is dirty | Current `handoff()` vs last successful save/load snapshot. Blank Stage 1 (all five IDs null) is clean. Any ID set with no snapshot yet is dirty. |
| Snapshot fields | The five `AgentStudioHandoffState` IDs only — the same object Save already writes. Field-by-field equality, not `JSON.stringify`. |
| Conflict actions | **Save first**, **Discard**, **Cancel**. Escape / backdrop = Cancel. Initial focus on Cancel so Enter does not save or discard. |
| Save first (bound) | Silent `PUT` with current name + current `handoff()`. Success then hydrates the *chosen* draft. Failure: global HTTP toast, do not hydrate. |
| Save first (unbound) | Existing Save-draft name dialog. Success then hydrates. Cancel aborts the load. |
| Discard | No separate wipe. Proceed to today’s `loadDraft` hydrate; `markClean()` after hydrate. |
| Rename | Header shows bound name + pencil. Name-only dialog, `PATCH { name }`. Does not `markClean()`. |
| Delete | Load-menu `⋯ → Delete`, shared danger confirm. Success: drop row, emit `draftDeleted`. If that id was bound, unbind name only — keep handoff and stepper. |
| Ownership | State service: snapshot + `isDirty`. Shell: conflict + rename + unbind. Load menu: delete + list. |

## Architecture

Three owners, same split as today’s save/load.

```
Load menu ──draftSelected──► Shell.loadDraft
                │                 │
                │                 ├─ !isDirty → getDraft → hydrateFromDraft → markClean
                │                 └─ isDirty  → DraftConflictDialog
                │                                   ├─ cancel  → no-op
                │                                   ├─ discard → getDraft → hydrate → markClean
                │                                   └─ save    → PUT or Save-draft dialog, then hydrate
                │
                └──draftDeleted──► Shell: if id === currentDraftId → setCurrentDraft(null, null)
```

### `AgentStudioStateService`

Add:

- Private `lastSavedHandoff` signal, starts `null`.
- `readonly isDirty = computed(...)` (same style as `canAdvance`):
  - If `lastSavedHandoff` is `null`: `true` iff any of the five IDs is non-null.
  - Else: `true` iff any of the five IDs differs from the snapshot (strict `===` per field).
- `markClean(): void` — copies current `handoff()` into `lastSavedHandoff`.
- `reset()` also sets `lastSavedHandoff` to `null`.

`markClean()` runs after: successful Save (create or update, including Save-first), and after a successful load hydrate. Rename does **not** call it. Unbind-on-delete does **not** call it (the journey is now unsaved local work).

No HTTP and no dialogs in the state service.

### `AgentStudioShellComponent`

- **`loadDraft(draftId)`** — if `!state.isDirty()`, keep today’s fetch/hydrate. If dirty, open `DraftConflictDialogComponent` first; only then fetch. `loadingDraft()` stays false while the dialog is open (the Load trigger stays enabled so Cancel is not stranded behind a disabled menu). Set `loadingDraft()` true only once hydration HTTP starts, same as today.
- **Save first, bound** — `api.updateDraft(currentDraftId, { name: currentDraftName, payload: { ...handoff() } })`. On success: `setCurrentDraft` + `markClean()`, then existing hydrate of `draftId`. On failure: do not hydrate; rely on the global HTTP error toast.
- **Save first, unbound** — open the existing `SaveDraftDialogComponent` (same options as the header Save button). If it closes with a summary, bind + `markClean()` + hydrate `draftId`. If it closes empty, abort. Refactor `openSaveDraftDialog()` to return the dialog’s `afterClosed()` observable so the header button and Save-first can share one open path.
- **Header** — when `currentDraftId()` is set, render the bound name and a pencil (`aria-label="Rename draft"`) to the left of Save/Load. Hidden when unbound.
- **Rename** — pencil opens `RenameDraftDialogComponent` with `{ draftId, initialName }`. Success: `setCurrentDraft(id, newName)` only.
- **`draftDeleted`** — if `id === currentDraftId()`, `setCurrentDraft(null, null)`. Do not `reset()`, do not `markClean()`.

`hydrateFromDraft` remains the only hydration path and must call `markClean()` after applying payload IDs (so a just-loaded draft is clean even if Discard skipped a save).

Loading the currently bound draft is not a special case: dirty → same prompt; clean → hydrate (refresh).

### `LoadDraftMenuComponent`

Each row stays a select target. A `more_vert` button uses `$event.stopPropagation()` and a nested `mat-menu` with **Delete**. The ⋯ click must not emit `draftSelected`.

Delete opens `ConfirmDialogComponent`:

- title: `Delete this draft?`
- message: the draft’s name, plus that this cannot be undone.
- confirmLabel: `Delete`
- cancelLabel: `Keep draft`
- variant: `danger`

On confirm, `api.deleteDraft(id)`. Success: remove that id from `drafts()`, emit `draftDeleted`. Failure: set `error()` via `extractErrorDetail` (same helper as list-fetch); leave the row. A delete in flight disables that row’s ⋯ only.

Material will typically close the menus when Delete is chosen; that is acceptable. The next `onOpened()` already refetches page 1, so a deleted draft will not reappear. Local splice keeps `drafts()` consistent if the menu is still open.

### New: `DraftConflictDialogComponent`

Studio-only three-action dialog. Do not extend `ConfirmDialogComponent`.

- Title: `Unsaved changes`
- Message: `You have unsaved changes — save them first, or discard?`
- Actions: **Cancel** (text, initial focus), **Discard** (`warn`), **Save first** (`primary`)
- Result: `'save' | 'discard' | undefined` (`undefined` = Cancel, Escape, or backdrop)
- `disableClose: false` so Escape/backdrop cancel the load

### New: `RenameDraftDialogComponent`

Mirrors `SaveDraftDialogComponent` (dialog owns HTTP, stays open on error, busy-guards cancel) but:

- `PATCH` via `api.renameDraft(draftId, name)` only — no payload.
- Title: `Rename draft`
- Confirm label: `Rename`
- Blank/whitespace name → `Name is required.`
- `disableClose: true` and `closeOnNavigation: false` while following the same busy-cancel rule as Save draft

## Error handling

| Failure | Surface | Session |
|---|---|---|
| Save-first `PUT` | Global HTTP toast | Unchanged; chosen draft not loaded |
| Unbound Save-draft dialog | Existing in-dialog error | Load aborted until success |
| `getDraft` after a chosen action | Existing global toast (today) | Unchanged (`loadingDraft()` false) |
| Rename `PATCH` | In-dialog error, stay open | Name unchanged |
| Delete `DELETE` | Load-menu `error()` | Row remains; bind unchanged |

No new shell banner.

## Testing

Vitest, ≥90% line coverage on every new or modified file.

- **State:** blank session not dirty; setting an ID makes dirty; `markClean()` clears it; a later ID change makes dirty again; `reset()` returns to clean-blank; rename-equivalent `setCurrentDraft` does not by itself change `isDirty()`.
- **Shell `loadDraft`:** clean → `getDraft`, no conflict dialog; dirty + Cancel → no `getDraft`; dirty + Discard → `getDraft` + hydrate + clean; dirty + Save first (bound) → `updateDraft` then `getDraft`; unbound Save-first cancel → no `getDraft`; Save-first `PUT` error → no hydrate. `markClean()` runs after IDs are applied in `hydrateFromDraft`, not after the async furthest-stage `getProcess` (dirty is ID-only).
- **Header:** name/pencil absent when unbound, present when bound; pencil opens rename dialog; rename success updates the label; `draftDeleted` of the current id unbinds without clearing handoff IDs.
- **Load menu:** ⋯ does not emit `draftSelected`; Delete confirm cancel → no `deleteDraft`; confirm → `deleteDraft` + row gone + `draftDeleted`; list-fetch error path unchanged.
- **Conflict dialog:** closes with `'save'`, `'discard'`, or `undefined`.
- **Rename dialog:** PATCH + required-name validation + stay-open-on-error (mirror Save-draft tests).

## File map

| File | Change |
|---|---|
| `user-interface/src/app/services/agent-studio-state.service.ts` (+ spec) | Snapshot, `isDirty` computed, `markClean()`, `reset()` |
| `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.ts` (+ html/scss/spec) | Conflict gate, header name/pencil, rename, `draftDeleted` |
| `user-interface/src/app/components/agent-team-studio/agent-studio-shell/load-draft-menu/` | ⋯ menu, delete confirm, `draftDeleted` |
| `.../draft-conflict-dialog/` (new) | Three-action prompt |
| `.../rename-draft-dialog/` (new) | Name-only PATCH dialog |

No backend files.
