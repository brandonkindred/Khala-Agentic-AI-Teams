# Agent Studio Load-Conflict, Rename, and Delete Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agent Studio load never silently overwrites unsaved handoff edits (Save first / Discard / Cancel), and the header/Load menu gain rename and delete affordances.

**Architecture:** `AgentStudioStateService` owns a last-saved handoff snapshot and `isDirty`. The shell gates `loadDraft` on a Studio-only three-action dialog, shows the bound name + pencil, and unbinds on `draftDeleted`. The Load menu owns per-row delete (confirm + `DELETE` + splice). Backend `PATCH`/`DELETE` already exist.

**Tech Stack:** Angular 19, Angular Material dialogs/menus, Vitest, RxJS

**Spec:** `docs/superpowers/specs/2026-08-12-agent-studio-draft-conflict-rename-delete-design.md`

**Worktree:** `/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/5914-unsaved-edit-conflict` on `feature/5914-unsaved-edit-conflict`. Do all work there.

## Global Constraints

- Follow the approved design spec exactly — no `localStorage`, no partial-work payload fields, no backend changes, do not turn `ConfirmDialogComponent` into a three-action dialog
- Design-by-Contract docstrings (`Preconditions:` / `Postconditions:` / `Invariants:` where relevant) on every new public function/method/module
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Vitest; ≥90% line coverage on every new or modified file
- Angular 19 standalone components; OnPush; existing Save-draft dialog patterns (dialog owns HTTP, stays open on error, busy-guards cancel)
- `docs/superpowers/` is gitignored — `git add -f` when committing files under it

## File map

| File | Role |
|---|---|
| `user-interface/src/app/services/agent-studio-state.service.ts` (+ `.spec.ts`) | last-saved snapshot, `isDirty`, `markClean()`, `reset()` |
| `.../agent-studio-shell/draft-conflict-dialog/` (new) | three-action Save first / Discard / Cancel |
| `.../agent-studio-shell/rename-draft-dialog/` (new) | name-only `PATCH` dialog |
| `.../agent-studio-shell/load-draft-menu/` | `⋯ → Delete`, confirm, `draftDeleted` |
| `.../agent-studio-shell/agent-studio-shell.component.ts` (+ html/scss/spec) | conflict gate, `markClean` on save/hydrate, header name/pencil, unbind |

---

### Task 1: Dirty tracking on `AgentStudioStateService`

**Files:**
- Modify: `user-interface/src/app/services/agent-studio-state.service.ts`
- Test: `user-interface/src/app/services/agent-studio-state.service.spec.ts`

**Interfaces:**
- Consumes: existing `handoff()` computed (`AgentStudioHandoffState`)
- Produces:
  - `readonly isDirty: Signal<boolean>` — `isDirty()` is `true` when `lastSavedHandoff` is `null` and any of the five IDs is non-null, or when any ID differs from the snapshot (`===` per field)
  - `markClean(): void` — copies current `handoff()` into `lastSavedHandoff`
  - `reset()` also sets `lastSavedHandoff` to `null`

- [ ] **Step 1: Write the failing tests**

Append to `agent-studio-state.service.spec.ts` (inside the existing `describe`):

```typescript
  describe('dirty tracking', () => {
    it('starts clean on a blank session', () => {
      expect(service.isDirty()).toBe(false);
    });

    it('becomes dirty when any handoff id is set and no snapshot exists yet', () => {
      service.setRegistryAgentId('reg-1');
      expect(service.isDirty()).toBe(true);
    });

    it('markClean makes the current handoff clean', () => {
      service.setTeamId('team-1');
      service.markClean();
      expect(service.isDirty()).toBe(false);
    });

    it('becomes dirty again when an id changes after markClean', () => {
      service.setTeamId('team-1');
      service.markClean();
      service.setTeamId('team-2');
      expect(service.isDirty()).toBe(true);
    });

    it('reset returns to a clean blank session', () => {
      service.setRegistryAgentId('reg-1');
      service.markClean();
      service.setTeamId('team-1');
      service.reset();
      expect(service.isDirty()).toBe(false);
      expect(service.handoff()).toEqual({
        registryAgentId: null,
        teamId: null,
        processId: null,
        personaId: null,
        draftAgentId: null,
      });
    });

    it('setCurrentDraft does not by itself change isDirty', () => {
      service.setRegistryAgentId('reg-1');
      service.markClean();
      service.setCurrentDraft('d-1', 'My draft');
      expect(service.isDirty()).toBe(false);
    });

    it('compares all five ids, not just the one that was last written', () => {
      service.setRegistryAgentId('reg-1');
      service.setDraftAgentId('draft-1');
      service.markClean();
      service.setPersonaId('persona-1');
      expect(service.isDirty()).toBe(true);
    });
  });
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd user-interface && npx vitest run src/app/services/agent-studio-state.service.spec.ts
```

Expected: FAIL — `service.isDirty is not a function` (or `isDirty` undefined).

- [ ] **Step 3: Write minimal implementation**

In `agent-studio-state.service.ts`, add two file-private helpers above the class (next to `STAGE_COUNT`):

```typescript
function handoffHasAnyId(h: AgentStudioHandoffState): boolean {
  return (
    h.registryAgentId != null ||
    h.teamId != null ||
    h.processId != null ||
    h.personaId != null ||
    h.draftAgentId != null
  );
}

function handoffEquals(a: AgentStudioHandoffState, b: AgentStudioHandoffState): boolean {
  return (
    a.registryAgentId === b.registryAgentId &&
    a.teamId === b.teamId &&
    a.processId === b.processId &&
    a.personaId === b.personaId &&
    a.draftAgentId === b.draftAgentId
  );
}
```

Add `AgentStudioHandoffState` to the existing import from `../models/agent-studio.model`.

Inside the class, after the `currentDraftName` signal:

```typescript
  /**
   * Last handoff snapshot that was successfully saved or loaded. `null` until
   * the first `markClean()`. Used only by `isDirty`.
   */
  private readonly lastSavedHandoff = signal<AgentStudioHandoffState | null>(null);

  /**
   * Whether the current handoff differs from the last saved/loaded snapshot.
   *
   * Preconditions: none.
   * Postconditions: `false` on a blank session (`lastSavedHandoff` null and
   *   every id null); `true` if any id is non-null and there is no snapshot
   *   yet, or if any of the five ids differs from the snapshot.
   */
  readonly isDirty = computed(() => {
    const current = this.handoff();
    const saved = this.lastSavedHandoff();
    if (saved === null) return handoffHasAnyId(current);
    return !handoffEquals(current, saved);
  });
```

Add `markClean` next to `setCurrentDraft`:

```typescript
  /**
   * Record the current handoff as the last saved/loaded snapshot.
   *
   * Preconditions: none.
   * Postconditions: `isDirty()` is `false` until a later handoff-id write.
   */
  markClean(): void {
    this.lastSavedHandoff.set({ ...this.handoff() });
  }
```

In `reset()`, after `this.currentDraftName.set(null);` add:

```typescript
    this.lastSavedHandoff.set(null);
```

Update the `reset()` docstring Postconditions to mention `isDirty()` is `false`.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
cd user-interface && npx vitest run src/app/services/agent-studio-state.service.spec.ts
```

Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add user-interface/src/app/services/agent-studio-state.service.ts \
        user-interface/src/app/services/agent-studio-state.service.spec.ts
git commit -m "$(cat <<'EOF'
Track Agent Studio handoff dirty state against the last save/load snapshot.

EOF
)"
```

---

### Task 2: `DraftConflictDialogComponent`

**Files:**
- Create: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/draft-conflict-dialog/draft-conflict-dialog.component.ts`
- Create: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/draft-conflict-dialog/draft-conflict-dialog.component.html`
- Create: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/draft-conflict-dialog/draft-conflict-dialog.component.scss`
- Create: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/draft-conflict-dialog/draft-conflict-dialog.component.spec.ts`

**Interfaces:**
- Consumes: `MatDialogRef<DraftConflictDialogComponent, DraftConflictResult>`
- Produces:
  - `export type DraftConflictResult = 'save' | 'discard'`
  - `save(): void` closes with `'save'`
  - `discard(): void` closes with `'discard'`
  - `cancel(): void` closes with `undefined` (`ref.close()`)
  - No `MAT_DIALOG_DATA` — copy is fixed

- [ ] **Step 1: Write the failing tests**

Create `draft-conflict-dialog.component.spec.ts`:

```typescript
import { TestBed } from '@angular/core/testing';
import { MatDialogRef } from '@angular/material/dialog';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DraftConflictDialogComponent } from './draft-conflict-dialog.component';

function configure() {
  const ref = { close: vi.fn() };
  TestBed.configureTestingModule({
    imports: [DraftConflictDialogComponent],
    providers: [{ provide: MatDialogRef, useValue: ref }],
  });
  const fixture = TestBed.createComponent(DraftConflictDialogComponent);
  fixture.detectChanges();
  return { fixture, ref };
}

describe('DraftConflictDialogComponent', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('Save first closes with save', () => {
    const { fixture, ref } = configure();
    fixture.componentInstance.save();
    expect(ref.close).toHaveBeenCalledWith('save');
  });

  it('Discard closes with discard', () => {
    const { fixture, ref } = configure();
    fixture.componentInstance.discard();
    expect(ref.close).toHaveBeenCalledWith('discard');
  });

  it('Cancel closes with no result', () => {
    const { fixture, ref } = configure();
    fixture.componentInstance.cancel();
    expect(ref.close).toHaveBeenCalledWith();
  });

  it('renders the spec copy and three actions', () => {
    const { fixture } = configure();
    const text = fixture.nativeElement.textContent as string;
    expect(text).toContain('Unsaved changes');
    expect(text).toContain('You have unsaved changes — save them first, or discard?');
    expect(text).toContain('Save first');
    expect(text).toContain('Discard');
    expect(text).toContain('Cancel');
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd user-interface && npx vitest run src/app/components/agent-team-studio/agent-studio-shell/draft-conflict-dialog/draft-conflict-dialog.component.spec.ts
```

Expected: FAIL — cannot resolve `./draft-conflict-dialog.component`.

- [ ] **Step 3: Write minimal implementation**

`draft-conflict-dialog.component.ts`:

```typescript
import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatDialogModule, MatDialogRef } from '@angular/material/dialog';

/** Result of an explicit choice. Cancel / Escape / backdrop yield `undefined`. */
export type DraftConflictResult = 'save' | 'discard';

/**
 * Load-conflict prompt (UX spec §2.4). Three actions; Cancel is initially
 * focused so Enter does not save or discard.
 *
 * Invariants: this dialog performs no HTTP and does not read Studio state.
 */
@Component({
  selector: 'app-draft-conflict-dialog',
  standalone: true,
  imports: [MatDialogModule, MatButtonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './draft-conflict-dialog.component.html',
  styleUrl: './draft-conflict-dialog.component.scss',
})
export class DraftConflictDialogComponent {
  readonly ref = inject<MatDialogRef<DraftConflictDialogComponent, DraftConflictResult>>(
    MatDialogRef,
  );

  /**
   * Preconditions: none.
   * Postconditions: the dialog closes with `'save'`.
   */
  save(): void {
    this.ref.close('save');
  }

  /**
   * Preconditions: none.
   * Postconditions: the dialog closes with `'discard'`.
   */
  discard(): void {
    this.ref.close('discard');
  }

  /**
   * Preconditions: none.
   * Postconditions: the dialog closes with no result (`undefined`).
   */
  cancel(): void {
    this.ref.close();
  }
}
```

`draft-conflict-dialog.component.html`:

```html
<h2 mat-dialog-title class="draft-conflict-dialog__title">Unsaved changes</h2>

<mat-dialog-content class="draft-conflict-dialog__content">
  <p>You have unsaved changes — save them first, or discard?</p>
</mat-dialog-content>

<mat-dialog-actions align="end" class="draft-conflict-dialog__actions">
  <button mat-button type="button" cdkFocusInitial (click)="cancel()">Cancel</button>
  <button
    mat-flat-button
    type="button"
    color="warn"
    class="draft-conflict-dialog__discard"
    (click)="discard()"
  >
    Discard
  </button>
  <button mat-flat-button type="button" color="primary" (click)="save()">Save first</button>
</mat-dialog-actions>
```

`draft-conflict-dialog.component.scss`:

```scss
:host {
  display: block;
  --mdc-dialog-container-color: var(--kh-surface-2);
  --mdc-dialog-subhead-color: var(--kh-text-primary);
  --mdc-dialog-supporting-text-color: var(--kh-text-secondary);
}

.draft-conflict-dialog__title {
  color: var(--kh-text-primary);
  font-family: var(--kh-font-sans);
  font-weight: 600;
  font-size: var(--kh-text-lg);
  margin: 0;
  padding-bottom: var(--kh-space-2);
}

.draft-conflict-dialog__content {
  color: var(--kh-text-secondary);
  font-family: var(--kh-font-sans);
  font-size: var(--kh-text-base);
  line-height: 1.5;

  p {
    margin: 0;
  }
}

.draft-conflict-dialog__actions {
  padding-top: var(--kh-space-3);
  gap: var(--kh-space-2);
}

.draft-conflict-dialog__discard {
  background-color: var(--kh-warning) !important;
  color: var(--kh-surface-0) !important;
}
```

Do not import or modify `ConfirmDialogComponent`.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
cd user-interface && npx vitest run src/app/components/agent-team-studio/agent-studio-shell/draft-conflict-dialog/draft-conflict-dialog.component.spec.ts
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add user-interface/src/app/components/agent-team-studio/agent-studio-shell/draft-conflict-dialog
git commit -m "$(cat <<'EOF'
Add Agent Studio three-action unsaved-changes conflict dialog.

EOF
)"
```

---

### Task 3: `RenameDraftDialogComponent`

**Files:**
- Create: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/rename-draft-dialog/rename-draft-dialog.component.ts`
- Create: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/rename-draft-dialog/rename-draft-dialog.component.html`
- Create: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/rename-draft-dialog/rename-draft-dialog.component.scss`
- Create: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/rename-draft-dialog/rename-draft-dialog.component.spec.ts`

**Interfaces:**
- Consumes: `AgentStudioApiService.renameDraft(draftId: string, name: string): Observable<AgentStudioDraftSummary>`
- Produces:
  - `export interface RenameDraftDialogData { draftId: string; initialName: string }`
  - `export type RenameDraftDialogResult = AgentStudioDraftSummary`
  - `submit(): void` — `PATCH` then `ref.close(summary)`; blank name → `serverError = 'Name is required.'` and no HTTP; API error stays open
  - `cancel(): void` — no-op while `busy()`

- [ ] **Step 1: Write the failing tests**

Mirror `save-draft-dialog.component.spec.ts`. Create `rename-draft-dialog.component.spec.ts`:

```typescript
import { TestBed } from '@angular/core/testing';
import { MAT_DIALOG_DATA, MatDialogRef } from '@angular/material/dialog';
import { of, throwError } from 'rxjs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  RenameDraftDialogComponent,
  type RenameDraftDialogData,
} from './rename-draft-dialog.component';
import { AgentStudioApiService } from '../../../../services/agent-studio-api.service';
import type { AgentStudioDraftSummary } from '../../../../models/agent-studio.model';

const summary = (id: string, name: string): AgentStudioDraftSummary => ({
  draft_id: id,
  name,
  updated_at: '2026-01-01T00:00:00Z',
});

function configure(
  data: RenameDraftDialogData,
  renameDraft = vi.fn().mockReturnValue(of(summary(data.draftId, data.initialName))),
) {
  const ref = { close: vi.fn() };
  const api = { renameDraft };
  TestBed.configureTestingModule({
    imports: [RenameDraftDialogComponent],
    providers: [
      { provide: MAT_DIALOG_DATA, useValue: data },
      { provide: MatDialogRef, useValue: ref },
      { provide: AgentStudioApiService, useValue: api },
    ],
  });
  const fixture = TestBed.createComponent(RenameDraftDialogComponent);
  return { fixture, ref, api };
}

describe('RenameDraftDialogComponent', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('pre-fills the name from initialName', () => {
    const { fixture } = configure({ draftId: 'd-1', initialName: 'My draft' });
    expect(fixture.componentInstance.name()).toBe('My draft');
  });

  it('submit PATCHes the name and closes with the summary', () => {
    const renameDraft = vi.fn().mockReturnValue(of(summary('d-1', 'Renamed')));
    const { fixture, ref } = configure({ draftId: 'd-1', initialName: 'Old' }, renameDraft);
    fixture.componentInstance.name.set('Renamed');
    fixture.componentInstance.submit();
    expect(renameDraft).toHaveBeenCalledWith('d-1', 'Renamed');
    expect(ref.close).toHaveBeenCalledWith(summary('d-1', 'Renamed'));
  });

  it('a blank name sets a server error and does not call the API', () => {
    const { fixture, ref, api } = configure({ draftId: 'd-1', initialName: 'Old' });
    fixture.componentInstance.name.set('   ');
    fixture.componentInstance.submit();
    expect(fixture.componentInstance.serverError()).toBe('Name is required.');
    expect(api.renameDraft).not.toHaveBeenCalled();
    expect(ref.close).not.toHaveBeenCalled();
  });

  it('an API error surfaces serverError, resets busy, and keeps the dialog open', () => {
    const renameDraft = vi.fn().mockReturnValue(
      throwError(() => ({ error: { detail: 'Name already taken' } })),
    );
    const { fixture, ref } = configure({ draftId: 'd-1', initialName: 'Old' }, renameDraft);
    fixture.componentInstance.submit();
    expect(fixture.componentInstance.busy()).toBe(false);
    expect(fixture.componentInstance.serverError()).toBe('Name already taken');
    expect(ref.close).not.toHaveBeenCalled();
  });

  it('falls back to err.message, then a generic message, when no detail is present', () => {
    const renameDraft = vi.fn().mockReturnValue(throwError(() => ({ message: 'network down' })));
    const { fixture } = configure({ draftId: 'd-1', initialName: 'Old' }, renameDraft);
    fixture.componentInstance.submit();
    expect(fixture.componentInstance.serverError()).toBe('network down');
  });

  it('falls back to a generic message when the error has neither detail nor message', () => {
    const renameDraft = vi.fn().mockReturnValue(throwError(() => ({})));
    const { fixture } = configure({ draftId: 'd-1', initialName: 'Old' }, renameDraft);
    fixture.componentInstance.submit();
    expect(fixture.componentInstance.serverError()).toBe('Failed to rename draft.');
  });

  it('cancel closes with no result', () => {
    const { fixture, ref } = configure({ draftId: 'd-1', initialName: 'Old' });
    fixture.componentInstance.cancel();
    expect(ref.close).toHaveBeenCalledWith();
  });

  it('cancel is a no-op while a rename is in flight', () => {
    const renameDraft = vi.fn().mockReturnValue({ subscribe: () => undefined });
    const { fixture, ref } = configure({ draftId: 'd-1', initialName: 'Old' }, renameDraft);
    fixture.componentInstance.submit();
    expect(fixture.componentInstance.busy()).toBe(true);
    fixture.componentInstance.cancel();
    expect(ref.close).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd user-interface && npx vitest run src/app/components/agent-team-studio/agent-studio-shell/rename-draft-dialog/rename-draft-dialog.component.spec.ts
```

Expected: FAIL — cannot resolve `./rename-draft-dialog.component`.

- [ ] **Step 3: Write minimal implementation**

`rename-draft-dialog.component.ts`:

```typescript
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MAT_DIALOG_DATA, MatDialogModule, MatDialogRef } from '@angular/material/dialog';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { AgentStudioApiService } from '../../../../services/agent-studio-api.service';
import type { AgentStudioDraftSummary } from '../../../../models/agent-studio.model';

export interface RenameDraftDialogData {
  draftId: string;
  initialName: string;
}

export type RenameDraftDialogResult = AgentStudioDraftSummary;

/**
 * Name-only rename dialog. Owns its PATCH call and stays open on failure.
 *
 * Invariants: never writes the draft payload; `busy()` true means cancel is a no-op.
 */
@Component({
  selector: 'app-rename-draft-dialog',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatDialogModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatProgressSpinnerModule,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './rename-draft-dialog.component.html',
  styleUrl: './rename-draft-dialog.component.scss',
})
export class RenameDraftDialogComponent {
  private readonly api = inject(AgentStudioApiService);
  readonly data = inject<RenameDraftDialogData>(MAT_DIALOG_DATA);
  readonly ref = inject<MatDialogRef<RenameDraftDialogComponent, RenameDraftDialogResult>>(
    MatDialogRef,
  );

  readonly name = signal<string>('');
  readonly busy = signal<boolean>(false);
  readonly serverError = signal<string | null>(null);

  constructor() {
    this.name.set(this.data.initialName);
  }

  /**
   * Preconditions: none — a blank/whitespace name is ordinary invalid user input.
   * Postconditions: on success, `ref.close(summary)`. On failure, `busy() === false`,
   *   `serverError()` is non-empty, dialog stays open.
   */
  submit(): void {
    const trimmed = this.name().trim();
    if (!trimmed) {
      this.serverError.set('Name is required.');
      return;
    }
    this.busy.set(true);
    this.serverError.set(null);
    this.api.renameDraft(this.data.draftId, trimmed).subscribe({
      next: (summary) => this.ref.close(summary),
      error: (err) => {
        this.busy.set(false);
        this.serverError.set(err?.error?.detail ?? err?.message ?? 'Failed to rename draft.');
      },
    });
  }

  /** Preconditions: none. Postconditions: no-op while `busy()`; otherwise closes with no result. */
  cancel(): void {
    if (this.busy()) return;
    this.ref.close();
  }
}
```

`rename-draft-dialog.component.html`:

```html
<h2 mat-dialog-title>Rename draft</h2>

<mat-dialog-content class="rename-draft-dialog__content">
  <mat-form-field appearance="outline">
    <mat-label>Draft name</mat-label>
    <input
      matInput
      [ngModel]="name()"
      (ngModelChange)="name.set($event)"
      [disabled]="busy()"
      cdkFocusInitial
    />
  </mat-form-field>

  @if (serverError()) {
    <p class="rename-draft-dialog__error" role="alert">
      <mat-icon>error</mat-icon>
      {{ serverError() }}
    </p>
  }
</mat-dialog-content>

<mat-dialog-actions align="end">
  <button mat-button (click)="cancel()" [disabled]="busy()">Cancel</button>
  <button
    mat-flat-button
    color="primary"
    (click)="submit()"
    [disabled]="busy() || !name().trim()"
  >
    @if (busy()) {
      <mat-spinner diameter="18" />
    } @else {
      Rename
    }
  </button>
</mat-dialog-actions>
```

`rename-draft-dialog.component.scss`:

```scss
.rename-draft-dialog {
  &__content {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 360px;
  }

  &__error {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    margin: 0;
    color: var(--mat-sys-error, #b00020);
    font-size: 0.85rem;

    mat-icon {
      font-size: 18px;
      height: 18px;
      width: 18px;
    }
  }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
cd user-interface && npx vitest run src/app/components/agent-team-studio/agent-studio-shell/rename-draft-dialog/rename-draft-dialog.component.spec.ts
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add user-interface/src/app/components/agent-team-studio/agent-studio-shell/rename-draft-dialog
git commit -m "$(cat <<'EOF'
Add Agent Studio name-only rename-draft dialog.

EOF
)"
```

---

### Task 4: Load-menu per-row delete

**Files:**
- Modify: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/load-draft-menu/load-draft-menu.component.ts`
- Modify: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/load-draft-menu/load-draft-menu.component.html`
- Modify: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/load-draft-menu/load-draft-menu.component.scss`
- Test: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/load-draft-menu/load-draft-menu.component.spec.ts`

**Interfaces:**
- Consumes: `AgentStudioApiService.deleteDraft`, `ConfirmDialogComponent` / `ConfirmDialogData`
- Produces: `@Output() readonly draftDeleted = new EventEmitter<string>()`
- `confirmDelete(draft: AgentStudioDraftSummary, event?: Event): void` — `event?.stopPropagation()`, danger confirm, on true `deleteDraft`, splice row, emit `draftDeleted`
- `deletingId = signal<string | null>(null)` — disables that row’s ⋯ while in flight

- [ ] **Step 1: Write the failing tests**

Extend `configure()` so the API fake includes `deleteDraft`, and add `NoopAnimationsModule` (nested `mat-menu` needs it). Spy `MatDialog.prototype.open` in a nested `describe('delete')` — the same prototype-spy pattern as the shell Save tests, because `{ provide: MatDialog, useValue }` does not reliably reach `providedIn: 'root'`.

Replace the existing `configure` helper with:

```typescript
import { MatDialog } from '@angular/material/dialog';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';

function configure(
  listDrafts = vi.fn().mockReturnValue(of([])),
  deleteDraft = vi.fn().mockReturnValue(of({ draft_id: 'd-1', status: 'deleted' })),
) {
  const api = { listDrafts, deleteDraft };
  TestBed.configureTestingModule({
    imports: [LoadDraftMenuComponent, NoopAnimationsModule],
    providers: [{ provide: AgentStudioApiService, useValue: api }],
  });
  const fixture = TestBed.createComponent(LoadDraftMenuComponent);
  return { fixture, api };
}
```

Existing tests that call `configure(listDrafts)` still work.

Add a nested describe. `afterEach` already in the file? The current spec has no afterEach — add `openSpy.mockRestore()` in this describe's afterEach, and `TestBed.resetTestingModule()` if missing.

```typescript
  describe('delete', () => {
    let openSpy: ReturnType<typeof vi.spyOn<MatDialog, 'open'>>;

    beforeEach(() => {
      openSpy = vi.spyOn(MatDialog.prototype, 'open');
    });
    afterEach(() => {
      openSpy.mockRestore();
    });

    it('confirmDelete cancel does not call deleteDraft or emit draftDeleted', () => {
      openSpy.mockReturnValue({ afterClosed: () => of(false) } as unknown as ReturnType<MatDialog['open']>);
      const { fixture, api } = configure();
      const spy = vi.fn();
      fixture.componentInstance.draftDeleted.subscribe(spy);
      fixture.componentInstance.confirmDelete(summary('d-1', 'A'));
      expect(api.deleteDraft).not.toHaveBeenCalled();
      expect(spy).not.toHaveBeenCalled();
    });

    it('confirmDelete confirm deletes, drops the row, and emits draftDeleted', () => {
      openSpy.mockReturnValue({ afterClosed: () => of(true) } as unknown as ReturnType<MatDialog['open']>);
      const deleteDraft = vi.fn().mockReturnValue(of({ draft_id: 'd-1', status: 'deleted' }));
      const { fixture } = configure(
        vi.fn().mockReturnValue(of([summary('d-1', 'A'), summary('d-2', 'B')])),
        deleteDraft,
      );
      fixture.componentInstance.onOpened();
      const spy = vi.fn();
      fixture.componentInstance.draftDeleted.subscribe(spy);
      fixture.componentInstance.confirmDelete(summary('d-1', 'A'));
      expect(deleteDraft).toHaveBeenCalledWith('d-1');
      expect(fixture.componentInstance.drafts().map((d) => d.draft_id)).toEqual(['d-2']);
      expect(spy).toHaveBeenCalledWith('d-1');
    });

    it('confirmDelete API failure sets error() and leaves the row', () => {
      openSpy.mockReturnValue({ afterClosed: () => of(true) } as unknown as ReturnType<MatDialog['open']>);
      const deleteDraft = vi.fn().mockReturnValue(throwError(() => ({ error: { detail: 'nope' } })));
      const { fixture } = configure(vi.fn().mockReturnValue(of([summary('d-1', 'A')])), deleteDraft);
      fixture.componentInstance.onOpened();
      const spy = vi.fn();
      fixture.componentInstance.draftDeleted.subscribe(spy);
      fixture.componentInstance.confirmDelete(summary('d-1', 'A'));
      expect(fixture.componentInstance.error()).toBe('nope');
      expect(fixture.componentInstance.drafts()).toEqual([summary('d-1', 'A')]);
      expect(spy).not.toHaveBeenCalled();
    });

    it('confirmDelete stops the click from selecting the row', () => {
      openSpy.mockReturnValue({ afterClosed: () => of(false) } as unknown as ReturnType<MatDialog['open']>);
      const { fixture, api } = configure();
      const selected = vi.fn();
      fixture.componentInstance.draftSelected.subscribe(selected);
      const event = { stopPropagation: vi.fn() } as unknown as Event;
      fixture.componentInstance.confirmDelete(summary('d-1', 'A'), event);
      expect(event.stopPropagation).toHaveBeenCalled();
      expect(selected).not.toHaveBeenCalled();
      expect(api.deleteDraft).not.toHaveBeenCalled();
    });
  });
```

Update the existing `onOpened` docstring test comment that says “or deleted, once #5914 lands” — rewrite without an issue number: “or deleted since the menu was last opened”.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd user-interface && npx vitest run src/app/components/agent-team-studio/agent-studio-shell/load-draft-menu/load-draft-menu.component.spec.ts
```

Expected: FAIL — `draftDeleted` / `confirmDelete` undefined.

- [ ] **Step 3: Write minimal implementation**

In `load-draft-menu.component.ts`:

- Import `MatDialog`, `ConfirmDialogComponent`, `ConfirmDialogData`.
- Add `MatDialogModule` to `imports` (needed if the template ever referenced it; the confirm is opened in code — still import `MatDialog` via inject).
- `private readonly dialog = inject(MatDialog);`
- `@Output() readonly draftDeleted = new EventEmitter<string>();`
- `readonly deletingId = signal<string | null>(null);`

```typescript
  /**
   * Open the danger confirm, then DELETE the draft.
   *
   * Preconditions: `draft.draft_id` is a non-empty id from a rendered row.
   * Postconditions: on confirm+success, that id is absent from `drafts()` and
   *   `draftDeleted` emitted once. On cancel or failure, `drafts()` unchanged
   *   and `draftDeleted` not emitted. `event` is stopPropagation'd when passed
   *   so the parent row does not select.
   */
  confirmDelete(draft: AgentStudioDraftSummary, event?: Event): void {
    event?.stopPropagation();
    if (this.deletingId()) return;
    const data: ConfirmDialogData = {
      title: 'Delete this draft?',
      message: `"${draft.name}" will be permanently deleted. This cannot be undone.`,
      confirmLabel: 'Delete',
      cancelLabel: 'Keep draft',
      variant: 'danger',
    };
    this.dialog
      .open<ConfirmDialogComponent, ConfirmDialogData, boolean>(ConfirmDialogComponent, { data })
      .afterClosed()
      .subscribe((confirmed) => {
        if (confirmed !== true) return;
        this.deletingId.set(draft.draft_id);
        this.api.deleteDraft(draft.draft_id).subscribe({
          next: () => {
            this.drafts.update((rows) => rows.filter((row) => row.draft_id !== draft.draft_id));
            this.deletingId.set(null);
            this.draftDeleted.emit(draft.draft_id);
          },
          error: (err) => {
            this.deletingId.set(null);
            this.error.set(extractErrorDetail(err, 'Failed to delete draft.'));
          },
        });
      });
  }
```

In `load-draft-menu.component.html`, change each row to keep click-to-select and add a nested ⋯ menu. Put the nested `mat-menu` inside the `@for`:

```html
  @for (draft of drafts(); track draft.draft_id) {
    <button mat-menu-item type="button" class="load-draft-menu__row" (click)="select(draft.draft_id)">
      <span class="load-draft-menu__body">
        <span class="load-draft-menu__name">{{ draft.name }}</span>
        <span class="load-draft-menu__when">{{ draft.updated_at | date: 'medium' }}</span>
      </span>
      <button
        mat-icon-button
        type="button"
        class="load-draft-menu__overflow"
        [matMenuTriggerFor]="rowMenu"
        [disabled]="deletingId() === draft.draft_id"
        (click)="$event.stopPropagation()"
        [attr.aria-label]="'Actions for ' + draft.name"
      >
        <mat-icon>more_vert</mat-icon>
      </button>
    </button>
    <mat-menu #rowMenu="matMenu">
      <button mat-menu-item type="button" (click)="confirmDelete(draft, $event)">
        <mat-icon>delete</mat-icon>
        Delete
      </button>
    </mat-menu>
  }
```

SCSS: `.load-draft-menu__row` is `display: flex; align-items: center; justify-content: space-between; gap: 8px;` so the ⋯ sits on the right. `.load-draft-menu__body` stays the stacked name/when block.

Rewrite the `onOpened` docstring: drop any issue-number mention; say the refetch also picks up deletes.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
cd user-interface && npx vitest run src/app/components/agent-team-studio/agent-studio-shell/load-draft-menu/load-draft-menu.component.spec.ts
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add user-interface/src/app/components/agent-team-studio/agent-studio-shell/load-draft-menu
git commit -m "$(cat <<'EOF'
Add per-row delete to the Agent Studio load-draft menu.

EOF
)"
```

---

### Task 5: `markClean` on save/hydrate; `openSaveDraftDialog` returns `afterClosed`

**Files:**
- Modify: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.ts`
- Test: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.spec.ts`

**Interfaces:**
- Consumes: `AgentStudioStateService.markClean()`, `isDirty`
- Produces:
  - `openSaveDraftDialog(): Observable<SaveDraftDialogResult | undefined>` — existing bind behavior plus `markClean()` on success; returns `ref.afterClosed()`
  - `hydrateFromDraft` calls `markClean()` after applying payload IDs (before `resolveFurthestStage`)

- [ ] **Step 1: Write the failing tests**

In `agent-studio-shell.component.spec.ts`, inside `describe('Save draft popover')`, add:

```typescript
    it('markClean after a successful save so the session is not dirty', () => {
      component.state.setRegistryAgentId('reg-1');
      expect(component.state.isDirty()).toBe(true);
      openSpy.mockReturnValue({ afterClosed: () => of(draftSummary()) } as unknown as ReturnType<MatDialog['open']>);
      component.openSaveDraftDialog();
      expect(component.state.isDirty()).toBe(false);
    });
```

Inside `describe('Load draft')`, add:

```typescript
    it('markClean after hydrate so a just-loaded draft is not dirty', () => {
      component.state.setRegistryAgentId('local-unsaved');
      expect(component.state.isDirty()).toBe(true);
      agentStudioApi.getDraft.mockReturnValue(of(draft({ registryAgentId: 'reg-1' })));
      component.loadDraft('d-1');
      expect(component.state.registryAgentId()).toBe('reg-1');
      expect(component.state.isDirty()).toBe(false);
    });
```

Note: this Load test still runs against today’s `loadDraft` (no conflict dialog yet). Because the session is dirty, Task 6 will start prompting — **this test must keep passing after Task 6**, which means Task 6’s conflict gate must not break a caller that… wait. After Task 6, dirty + loadDraft opens a dialog and does NOT getDraft until Discard/Save. This test would then fail: it sets dirty, calls loadDraft, expects hydrate.

**Do not add the load `markClean` test in this form.** Instead, in Task 5, only test save-path `markClean`, and test load-path `markClean` on a **clean** session (blank → load):

```typescript
    it('markClean after hydrate so a just-loaded draft is not dirty', () => {
      expect(component.state.isDirty()).toBe(false);
      agentStudioApi.getDraft.mockReturnValue(of(draft({ registryAgentId: 'reg-1' })));
      component.loadDraft('d-1');
      expect(component.state.registryAgentId()).toBe('reg-1');
      expect(component.state.isDirty()).toBe(false);
    });
```

Blank session is clean, so Task 6 will still hydrate immediately. After hydrate, IDs are set; without `markClean` the session would be dirty (snapshot still null, ids non-null). That is the assertion that fails before implementation.

Also add: `openSaveDraftDialog` returns an observable that emits the dialog result (header tests can ignore the return). Optional:

```typescript
    it('returns afterClosed so callers can chain on success', () => {
      openSpy.mockReturnValue({ afterClosed: () => of(draftSummary()) } as unknown as ReturnType<MatDialog['open']>);
      const emitted: unknown[] = [];
      component.openSaveDraftDialog().subscribe((v) => emitted.push(v));
      expect(emitted).toEqual([draftSummary()]);
    });
```

This fails today because the method returns `void` (`.subscribe` is not a function).

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd user-interface && npx vitest run src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.spec.ts
```

Expected: FAIL — `openSaveDraftDialog().subscribe` is not a function, and/or `isDirty()` still true after save/hydrate.

- [ ] **Step 3: Write minimal implementation**

Add `import { Observable } from 'rxjs';` to the shell component.

Change `openSaveDraftDialog` to:

```typescript
  /**
   * Open the Save-draft popover (spec §3.5).
   *
   * Preconditions: none — always safe to call.
   * Postconditions: on a successful save, `state.currentDraftId()` /
   *   `currentDraftName()` reflect the saved draft and `state.isDirty()` is
   *   false. On cancel or failure, state is unchanged. The returned
   *   observable is the dialog's `afterClosed()`.
   */
  openSaveDraftDialog(): Observable<SaveDraftDialogResult | undefined> {
    const data: SaveDraftDialogData = {
      draftId: this.state.currentDraftId(),
      initialName: this.state.currentDraftName(),
      payload: { ...this.state.handoff() },
    };
    const ref = this.dialog.open<SaveDraftDialogComponent, SaveDraftDialogData, SaveDraftDialogResult>(
      SaveDraftDialogComponent,
      { data, width: '420px', disableClose: true, closeOnNavigation: false },
    );
    const closed$ = ref.afterClosed();
    closed$.subscribe((result) => {
      if (!result) return;
      this.state.setCurrentDraft(result.draft_id, result.name);
      this.state.markClean();
    });
    return closed$;
  }
```

Keep the existing `disableClose` comment above the `open` call.

In `hydrateFromDraft`, after the five `set*` calls and **before** `this.resolveFurthestStage(token)`:

```typescript
    this.state.markClean();
```

Rewrite `loadDraft`’s docstring: remove the sentence that says no conflict check is performed and any issue-number mention. Say hydration still runs immediately when `!state.isDirty()`; the conflict gate lands in the next task. (Or wait until Task 6 to rewrite that docstring — either is fine as long as no issue numbers remain after Task 6.)

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
cd user-interface && npx vitest run src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.spec.ts
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.ts \
        user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.spec.ts
git commit -m "$(cat <<'EOF'
Mark Agent Studio handoff clean after a successful save or load hydrate.

EOF
)"
```

---

### Task 6: Conflict gate on `loadDraft`

**Files:**
- Modify: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.ts`
- Test: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.spec.ts`

**Interfaces:**
- Consumes: `DraftConflictDialogComponent`, `DraftConflictResult`, `state.isDirty()`, `api.updateDraft`, `openSaveDraftDialog()`
- Produces:
  - `loadDraft(draftId: string): void` — if dirty, open conflict dialog (`disableClose: false`); `loadingDraft()` stays false until `fetchAndHydrate` starts
  - `'discard'` → `fetchAndHydrate(draftId)`
  - `'save'` + bound → `updateDraft` then `fetchAndHydrate`; `updateDraft` error → no hydrate
  - `'save'` + unbound → `openSaveDraftDialog()`; truthy result → `fetchAndHydrate`; empty → abort
  - `undefined` → no-op

- [ ] **Step 1: Write the failing tests**

Extend the shell spec’s `agentStudioApi` fake with `updateDraft`:

```typescript
  let agentStudioApi: {
    cloneFromRegistry: ReturnType<typeof vi.fn>;
    saveAgent: ReturnType<typeof vi.fn>;
    listDrafts: ReturnType<typeof vi.fn>;
    getDraft: ReturnType<typeof vi.fn>;
    updateDraft: ReturnType<typeof vi.fn>;
  };
```

In `beforeEach`:

```typescript
      updateDraft: vi.fn(),
```

Add a nested `describe('Load draft conflict')` after the existing Load draft tests. Reuse the file’s `draft()` helper. Spy `MatDialog.prototype.open` like the Save tests (own `beforeEach`/`afterEach` mockRestore so they do not leak).

```typescript
  describe('Load draft conflict', () => {
    let openSpy: ReturnType<typeof vi.spyOn<MatDialog, 'open'>>;

    beforeEach(() => {
      openSpy = vi.spyOn(MatDialog.prototype, 'open');
    });
    afterEach(() => {
      openSpy.mockRestore();
    });

    it('hydrates immediately when clean and does not open the conflict dialog', () => {
      agentStudioApi.getDraft.mockReturnValue(of(draft({})));
      component.loadDraft('d-1');
      expect(openSpy).not.toHaveBeenCalled();
      expect(agentStudioApi.getDraft).toHaveBeenCalledWith('d-1');
    });

    it('Cancel leaves the session unchanged and does not getDraft', () => {
      component.state.setRegistryAgentId('local-1');
      openSpy.mockReturnValue({ afterClosed: () => of(undefined) } as unknown as ReturnType<MatDialog['open']>);
      component.loadDraft('d-1');
      expect(agentStudioApi.getDraft).not.toHaveBeenCalled();
      expect(component.state.registryAgentId()).toBe('local-1');
      expect(component.loadingDraft()).toBe(false);
    });

    it('Discard hydrates the chosen draft', () => {
      component.state.setRegistryAgentId('local-1');
      openSpy.mockReturnValue({ afterClosed: () => of('discard') } as unknown as ReturnType<MatDialog['open']>);
      agentStudioApi.getDraft.mockReturnValue(of(draft({ registryAgentId: 'reg-1' })));
      component.loadDraft('d-1');
      expect(component.state.registryAgentId()).toBe('reg-1');
      expect(component.state.isDirty()).toBe(false);
    });

    it('Save first when bound PUTs then hydrates the chosen draft', () => {
      component.state.setRegistryAgentId('local-1');
      component.state.setCurrentDraft('bound-1', 'Bound');
      openSpy.mockReturnValue({ afterClosed: () => of('save') } as unknown as ReturnType<MatDialog['open']>);
      agentStudioApi.updateDraft.mockReturnValue(
        of({ draft_id: 'bound-1', name: 'Bound', updated_at: '2026-01-01T00:00:00Z' }),
      );
      agentStudioApi.getDraft.mockReturnValue(of(draft({ registryAgentId: 'reg-1' })));
      component.loadDraft('d-1');
      expect(agentStudioApi.updateDraft).toHaveBeenCalledWith('bound-1', {
        name: 'Bound',
        payload: expect.objectContaining({ registryAgentId: 'local-1' }),
      });
      expect(agentStudioApi.getDraft).toHaveBeenCalledWith('d-1');
      expect(component.state.registryAgentId()).toBe('reg-1');
    });

    it('Save first PUT error does not hydrate', () => {
      component.state.setRegistryAgentId('local-1');
      component.state.setCurrentDraft('bound-1', 'Bound');
      openSpy.mockReturnValue({ afterClosed: () => of('save') } as unknown as ReturnType<MatDialog['open']>);
      agentStudioApi.updateDraft.mockReturnValue(throwError(() => ({ error: { detail: 'nope' } })));
      component.loadDraft('d-1');
      expect(agentStudioApi.getDraft).not.toHaveBeenCalled();
      expect(component.state.registryAgentId()).toBe('local-1');
    });

    it('Save first when unbound opens the save dialog; cancel aborts the load', () => {
      component.state.setRegistryAgentId('local-1');
      // First open = conflict (save). Second open = Save-draft dialog.
      openSpy
        .mockReturnValueOnce({ afterClosed: () => of('save') } as unknown as ReturnType<MatDialog['open']>)
        .mockReturnValueOnce({ afterClosed: () => of(undefined) } as unknown as ReturnType<MatDialog['open']>);
      component.loadDraft('d-1');
      expect(agentStudioApi.getDraft).not.toHaveBeenCalled();
      expect(component.state.registryAgentId()).toBe('local-1');
    });
  });
```

Import `throwError` from `rxjs` if the spec file does not already (it does).

Conflict dialog open config: assert in the Cancel test (or a dedicated one) that `disableClose` is not `true`:

```typescript
      const config = openSpy.mock.calls[0][1] as { disableClose?: boolean };
      expect(config.disableClose).not.toBe(true);
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd user-interface && npx vitest run src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.spec.ts
```

Expected: FAIL — dirty `loadDraft` still calls `getDraft` without opening the conflict dialog.

- [ ] **Step 3: Write minimal implementation**

Import `DraftConflictDialogComponent` and `DraftConflictResult`. Add the component to the shell `imports` array (required for `dialog.open` JIT in tests that do not stub it; opening via `MatDialog` does not require it in `imports`, but include it for consistency with `SaveDraftDialogComponent` which is also not in the template — Save is not in `imports` today, only opened via `dialog.open`. **Do not add either dialog to `imports` unless the template references it.** Follow Save-draft: open via `MatDialog` only.)

Replace `loadDraft` + extract `fetchAndHydrate` (today’s body):

```typescript
  /**
   * Load a saved draft and hydrate the session from it (spec §3.5 / §2.4).
   *
   * Preconditions: `draftId` names a draft the current user owns.
   * Postconditions: when `!isDirty()`, hydrates immediately. When dirty, opens
   *   the conflict dialog and `loadingDraft()` stays false until hydration
   *   HTTP starts. Cancel / Escape / backdrop: no HTTP, state unchanged.
   *   Discard: hydrate the chosen draft. Save first: persist current handoff
   *   then hydrate; a failed persist does not hydrate.
   */
  loadDraft(draftId: string): void {
    if (this.loadingDraft()) return;
    if (this.state.isDirty()) {
      this.resolveLoadConflict(draftId);
      return;
    }
    this.fetchAndHydrate(draftId);
  }

  private resolveLoadConflict(draftId: string): void {
    const ref = this.dialog.open<DraftConflictDialogComponent, void, DraftConflictResult>(
      DraftConflictDialogComponent,
      { width: '420px', disableClose: false },
    );
    ref.afterClosed().subscribe((choice) => {
      if (choice === 'discard') {
        this.fetchAndHydrate(draftId);
        return;
      }
      if (choice === 'save') {
        this.saveFirstThenHydrate(draftId);
      }
    });
  }

  private saveFirstThenHydrate(draftId: string): void {
    const boundId = this.state.currentDraftId();
    const boundName = this.state.currentDraftName();
    if (boundId && boundName) {
      this.api
        .updateDraft(boundId, { name: boundName, payload: { ...this.state.handoff() } })
        .subscribe({
          next: (summary) => {
            this.state.setCurrentDraft(summary.draft_id, summary.name);
            this.state.markClean();
            this.fetchAndHydrate(draftId);
          },
          error: () => {
            // Global HTTP interceptor toasts. Do not hydrate.
          },
        });
      return;
    }
    this.openSaveDraftDialog().subscribe((result) => {
      if (!result) return;
      this.fetchAndHydrate(draftId);
    });
  }

  private fetchAndHydrate(draftId: string): void {
    if (this.loadingDraft()) return;
    this.loadingDraft.set(true);
    const token = ++this.loadDraftToken;
    this.api.getDraft(draftId).subscribe({
      next: (draft) => {
        if (token !== this.loadDraftToken) return;
        this.hydrateFromDraft(draft, token);
      },
      error: () => {
        if (token !== this.loadDraftToken) return;
        this.loadingDraft.set(false);
      },
    });
  }
```

Private methods still need DbC docstrings (`Preconditions` / `Postconditions`).

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
cd user-interface && npx vitest run src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.spec.ts
```

Expected: PASS (existing Load tests stay green because they start from a clean blank session).

- [ ] **Step 5: Commit**

```bash
git add user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.ts \
        user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.spec.ts
git commit -m "$(cat <<'EOF'
Prompt save-first or discard before loading a draft over unsaved handoff edits.

EOF
)"
```

---

### Task 7: Header name, pencil rename, and unbind on delete

**Files:**
- Modify: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.ts`
- Modify: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.html`
- Modify: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.scss`
- Test: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.spec.ts`

**Interfaces:**
- Consumes: `RenameDraftDialogComponent`, `RenameDraftDialogData`, `RenameDraftDialogResult`, `LoadDraftMenuComponent.draftDeleted`
- Produces:
  - `openRenameDraftDialog(): void` — no-op when unbound; else open rename dialog (`disableClose: true`, `closeOnNavigation: false`); success → `setCurrentDraft(id, name)` only (no `markClean`)
  - `onDraftDeleted(draftId: string): void` — if `draftId === currentDraftId()`, `setCurrentDraft(null, null)`

- [ ] **Step 1: Write the failing tests**

Add `renameDraft` to the `agentStudioApi` fake (default `vi.fn()`), same as `updateDraft`.

New `describe('Bound draft header')`:

```typescript
  describe('Bound draft header', () => {
    let openSpy: ReturnType<typeof vi.spyOn<MatDialog, 'open'>>;

    beforeEach(() => {
      openSpy = vi.spyOn(MatDialog.prototype, 'open');
    });
    afterEach(() => {
      openSpy.mockRestore();
    });

    it('hides the name and pencil when no draft is bound', () => {
      expect(fixture.nativeElement.querySelector('.studio__current-draft')).toBeNull();
    });

    it('shows the bound name and a rename button', () => {
      component.state.setCurrentDraft('d-1', 'My draft');
      fixture.detectChanges();
      const root = fixture.nativeElement.querySelector('.studio__current-draft') as HTMLElement;
      expect(root.textContent).toContain('My draft');
      const btn = fixture.nativeElement.querySelector('.studio__rename-draft') as HTMLButtonElement;
      expect(btn.getAttribute('aria-label')).toBe('Rename draft');
    });

    it('pencil opens the rename dialog with the bound id and name', () => {
      component.state.setCurrentDraft('d-1', 'My draft');
      openSpy.mockReturnValue({ afterClosed: () => of(undefined) } as unknown as ReturnType<MatDialog['open']>);
      component.openRenameDraftDialog();
      const config = openSpy.mock.calls[0][1] as {
        data: { draftId: string; initialName: string };
        disableClose?: boolean;
        closeOnNavigation?: boolean;
      };
      expect(config.data).toEqual({ draftId: 'd-1', initialName: 'My draft' });
      expect(config.disableClose).toBe(true);
      expect(config.closeOnNavigation).toBe(false);
    });

    it('rename success updates the bound name without markClean', () => {
      component.state.setRegistryAgentId('reg-1');
      component.state.markClean();
      component.state.setCurrentDraft('d-1', 'Old');
      openSpy.mockReturnValue({
        afterClosed: () => of({ draft_id: 'd-1', name: 'New', updated_at: '2026-01-01T00:00:00Z' }),
      } as unknown as ReturnType<MatDialog['open']>);
      component.openRenameDraftDialog();
      expect(component.state.currentDraftName()).toBe('New');
      expect(component.state.isDirty()).toBe(false);
    });

    it('openRenameDraftDialog is a no-op when unbound', () => {
      openSpy.mockReturnValue({ afterClosed: () => of(undefined) } as unknown as ReturnType<MatDialog['open']>);
      component.openRenameDraftDialog();
      expect(openSpy).not.toHaveBeenCalled();
    });

    it('onDraftDeleted of the current id unbinds without clearing handoff', () => {
      component.state.setRegistryAgentId('reg-1');
      component.state.setCurrentDraft('d-1', 'My draft');
      component.onDraftDeleted('d-1');
      expect(component.state.currentDraftId()).toBeNull();
      expect(component.state.currentDraftName()).toBeNull();
      expect(component.state.registryAgentId()).toBe('reg-1');
    });

    it('onDraftDeleted of a different id leaves the bound draft alone', () => {
      component.state.setCurrentDraft('d-1', 'My draft');
      component.onDraftDeleted('other');
      expect(component.state.currentDraftId()).toBe('d-1');
    });
  });
```

Also assert the template wires `draftDeleted` (similar to the existing `draftSelected` test):

```typescript
    it('the Load-draft menu draftDeleted output calls onDraftDeleted', () => {
      component.state.setCurrentDraft('d-1', 'My draft');
      const menu = fixture.debugElement.query(By.directive(LoadDraftMenuComponent));
      menu.triggerEventHandler('draftDeleted', 'd-1');
      expect(component.state.currentDraftId()).toBeNull();
    });
```

That last test can live in the existing `Load draft` describe.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd user-interface && npx vitest run src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.spec.ts
```

Expected: FAIL — missing header markup / `openRenameDraftDialog` / `onDraftDeleted`.

- [ ] **Step 3: Write minimal implementation**

HTML — replace the header comment (no issue numbers) and insert the bound-name chip **before** the Save button:

```html
  <header class="studio__header">
    <h1 class="studio__title">Agent Studio</h1>
    <div class="studio__draft-actions">
      @if (state.currentDraftId()) {
        <span class="studio__current-draft">
          <span class="studio__current-draft-name">{{ state.currentDraftName() }}</span>
          <button
            mat-icon-button
            type="button"
            class="studio__rename-draft"
            aria-label="Rename draft"
            (click)="openRenameDraftDialog()"
          >
            <mat-icon>edit</mat-icon>
          </button>
        </span>
      }
      <button mat-stroked-button type="button" class="studio__draft-btn" (click)="openSaveDraftDialog()">Save draft</button>
      <app-load-draft-menu
        [busy]="loadingDraft()"
        (draftSelected)="loadDraft($event)"
        (draftDeleted)="onDraftDeleted($event)"
      />
    </div>
  </header>
```

The existing test `renders Save draft and Load draft, both enabled` queries `.studio__draft-btn` and expects length 2. The rename control is `mat-icon-button` with class `studio__rename-draft`, **not** `studio__draft-btn`, so that test stays green.

SCSS under `&__draft-actions`:

```scss
  &__current-draft {
    display: inline-flex;
    align-items: center;
    gap: var(--kh-space-1);
    min-width: 0;
  }

  &__current-draft-name {
    max-width: 16rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-weight: 600;
    color: var(--kh-text-primary);
  }
```

In the shell class, import rename dialog types. Add:

```typescript
  /**
   * Open the rename dialog for the bound draft.
   *
   * Preconditions: none — no-op when unbound or when the bound name is null.
   * Postconditions: on success, `currentDraftName()` matches the PATCH
   *   response; `isDirty()` is unchanged. On cancel/failure, state unchanged.
   */
  openRenameDraftDialog(): void {
    const draftId = this.state.currentDraftId();
    const initialName = this.state.currentDraftName();
    if (!draftId || initialName == null) return;
    const data: RenameDraftDialogData = { draftId, initialName };
    const ref = this.dialog.open<RenameDraftDialogComponent, RenameDraftDialogData, RenameDraftDialogResult>(
      RenameDraftDialogComponent,
      { data, width: '420px', disableClose: true, closeOnNavigation: false },
    );
    ref.afterClosed().subscribe((result) => {
      if (!result) return;
      this.state.setCurrentDraft(result.draft_id, result.name);
    });
  }

  /**
   * Handle a successful delete from the Load menu.
   *
   * Preconditions: `draftId` is a non-empty id.
   * Postconditions: if it was the bound draft, `currentDraftId()` /
   *   `currentDraftName()` are null and handoff ids are unchanged. Otherwise
   *   state is unchanged.
   */
  onDraftDeleted(draftId: string): void {
    if (this.state.currentDraftId() === draftId) {
      this.state.setCurrentDraft(null, null);
    }
  }
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
cd user-interface && npx vitest run src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.spec.ts \
  src/app/components/agent-team-studio/agent-studio-shell/load-draft-menu/load-draft-menu.component.spec.ts \
  src/app/components/agent-team-studio/agent-studio-shell/draft-conflict-dialog/draft-conflict-dialog.component.spec.ts \
  src/app/components/agent-team-studio/agent-studio-shell/rename-draft-dialog/rename-draft-dialog.component.spec.ts \
  src/app/services/agent-studio-state.service.spec.ts
```

Expected: PASS.

Then coverage on touched files:

```bash
cd user-interface && npx vitest run --coverage \
  src/app/services/agent-studio-state.service.spec.ts \
  src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.spec.ts \
  src/app/components/agent-team-studio/agent-studio-shell/load-draft-menu/load-draft-menu.component.spec.ts \
  src/app/components/agent-team-studio/agent-studio-shell/draft-conflict-dialog/draft-conflict-dialog.component.spec.ts \
  src/app/components/agent-team-studio/agent-studio-shell/rename-draft-dialog/rename-draft-dialog.component.spec.ts
```

Expected: ≥90% lines on each new/modified file. If a branch is unreachable, add a one-line `/* v8 ignore next */` with justification — do not lower the global threshold.

- [ ] **Step 5: Commit**

```bash
git add user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.ts \
        user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.html \
        user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.scss \
        user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.spec.ts
git commit -m "$(cat <<'EOF'
Show the bound draft name with rename and unbind on delete.

EOF
)"
```

---

## Spec coverage (self-review)

| Spec requirement | Task |
|---|---|
| Dirty = handoff snapshot vs last save/load; blank clean | Task 1 |
| Field-by-field equality, not `JSON.stringify` | Task 1 |
| Three-action conflict dialog; Cancel focused; Escape/backdrop cancel | Task 2 + Task 6 (`disableClose: false`) |
| Save first bound = silent PUT then hydrate | Task 6 |
| Save first unbound = Save-draft dialog; cancel aborts | Task 5 return value + Task 6 |
| Discard = hydrate, no separate wipe | Task 6 |
| `markClean` after save and after hydrate IDs | Task 5 |
| Rename name-only PATCH; no `markClean` | Task 3 + Task 7 |
| Header name + pencil when bound | Task 7 |
| Load-menu `⋯ → Delete` + danger confirm | Task 4 |
| Delete bound draft unbinds, keeps handoff | Task 7 |
| Delete failure stays in menu `error()` | Task 4 |
| Save-first PUT failure: toast, no hydrate | Task 6 |
| `loadingDraft()` false while conflict dialog open | Task 6 |
| No `localStorage` / backend / shared confirm rewrite | all tasks |
| No GitHub issue numbers in new writing | Task 4/5/6 docstring rewrites |
