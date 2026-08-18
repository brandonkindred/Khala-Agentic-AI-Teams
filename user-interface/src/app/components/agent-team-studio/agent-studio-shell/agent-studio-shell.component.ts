import { ChangeDetectionStrategy, Component, Injector, computed, inject, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { MatButtonModule } from '@angular/material/button';
import { MatDialog, MatDialogModule } from '@angular/material/dialog';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { ActivatedRoute, NavigationEnd, Router, RouterOutlet } from '@angular/router';
import { Observable, filter, map } from 'rxjs';
import type { AgentStudioDraft } from '../../../models/agent-studio.model';
import { STAGE_INDEX, STUDIO_STAGES } from '../../../models/agent-studio.model';
import { HasUnsavedChanges } from '../../../core/unsaved-changes.guard';
import { AgentStudioFacade } from '../../../services/agent-studio.facade';
import { AgentStudioStateService, handoffEquals } from '../../../services/agent-studio-state.service';
import { AgenticTeamApiService } from '../../../services/agentic-team-api.service';
import {
  DraftConflictDialogComponent,
  type DraftConflictResult,
} from './draft-conflict-dialog/draft-conflict-dialog.component';
import { LoadDraftMenuComponent } from './load-draft-menu/load-draft-menu.component';
import {
  RenameDraftDialogComponent,
  type RenameDraftDialogData,
  type RenameDraftDialogResult,
} from './rename-draft-dialog/rename-draft-dialog.component';
import {
  SaveDraftDialogComponent,
  type SaveDraftDialogData,
  type SaveDraftDialogResult,
} from './save-draft-dialog/save-draft-dialog.component';

/** `draft.payload` is an opaque, backend-unvalidated blob (spec §3.5) — never
 *  trust a field's type without checking it first. */
function asNullableString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

/**
 * Agent Studio shell — the single `/agent-studio` surface (spec §2.1). Renders
 * the forward-only 4-stage stepper and header; the active stage itself is
 * rendered by the routed `AgentStudioStageHostComponent` child (default child
 * route) via `RouterOutlet` — the persona-run audit is a sibling child route.
 * Owns one `AgentStudioStateService` per session (provided here, not at root,
 * so each visit starts clean). `AgentStudioFacade` is provided alongside it
 * so the facade's injector can resolve this session's state service (it is
 * not a root singleton either).
 */
@Component({
  selector: 'app-agent-studio-shell',
  standalone: true,
  imports: [MatButtonModule, MatDialogModule, MatIconModule, MatTooltipModule, LoadDraftMenuComponent, RouterOutlet],
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [AgentStudioStateService, AgentStudioFacade],
  templateUrl: './agent-studio-shell.component.html',
  styleUrl: './agent-studio-shell.component.scss',
})
export class AgentStudioShellComponent implements HasUnsavedChanges {
  readonly state = inject(AgentStudioStateService);
  private readonly dialog = inject(MatDialog);
  private readonly facade = inject(AgentStudioFacade);
  private readonly injector = inject(Injector);
  private readonly agenticTeamApi = inject(AgenticTeamApiService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  /**
   * True when the active child route sets `data.hideStudioFooter`.
   *
   * Preconditions: this component is the routed `/agent-studio` parent.
   * Postconditions: `true` iff the deepest activated child snapshot has
   *   `hideStudioFooter === true`; `false` when there is no child (unit tests
   *   that construct the shell without navigating).
   */
  readonly hideFooter = toSignal(
    this.router.events.pipe(
      filter((e): e is NavigationEnd => e instanceof NavigationEnd),
      map(() => this.childHidesFooter()),
    ),
    // Router.events does not replay the NavigationEnd that created this
    // component, so seed from the already-activated child snapshot. A hardcoded
    // `false` would flash the footer on a direct load of the audit child.
    { initialValue: this.childHidesFooter() },
  );

  /**
   * Read `data.hideStudioFooter` from the deepest activated child.
   *
   * Preconditions: none — safe when there is no child (shell constructed
   *   without navigating).
   * Postconditions: `true` iff that child's snapshot has
   *   `hideStudioFooter === true`. Walks `firstChild` because the audit
   *   route is nested under this shell; `snapshot` may be missing while
   *   the outlet is still activating.
   */
  private childHidesFooter(): boolean {
    let child = this.route.firstChild;
    while (child?.firstChild) {
      child = child.firstChild;
    }
    // `firstChild` can exist before `snapshot` is attached during outlet activation.
    return child?.snapshot?.data['hideStudioFooter'] === true;
  }

  /** True while a Load-draft selection is being fetched and hydrated. */
  readonly loadingDraft = signal(false);
  /** Bumped on every `loadDraft` call; lets a superseded call's late-arriving
   *  responses (`loadDraft`, the nested `getProcess` check) recognize they're
   *  stale and no-op instead of corrupting a newer load. */
  private loadDraftToken = 0;
  /** The forward-only stage list rendered by the stepper. */
  readonly stages = STUDIO_STAGES;

  /**
   * Descriptor of the stage currently shown (drives the active stage view).
   * Defensive: `activeStage()` is range-guarded by the state service, so this
   * is always in range — but fail loud rather than hand the template an
   * `undefined` stage if that invariant is ever broken.
   */
  readonly activeStageDef = computed(() => {
    const idx = this.state.activeStage();
    /* v8 ignore next 3 -- defensive: activeStage is range-guarded by AgentStudioStateService, so this branch is unreachable */
    if (idx < 0 || idx >= this.stages.length) {
      throw new RangeError(`activeStageDef: active stage index ${idx} is out of range`);
    }
    return this.stages[idx];
  });

  /**
   * Whether the shell's forward affordance is disabled for the current stage.
   * Build ("Test this agent →") and Test ("Add to team →") require an agent to
   * have been selected. Compose ("Test this team →") requires a fully-staffed
   * roster on a `complete` process (spec §3, Stage 3 handoff gate) — you can't
   * test-drive a team that isn't staffed or whose process isn't finished.
   */
  readonly forwardDisabled = computed(() => {
    const key = this.activeStageDef().key;
    if (key === 'build' || key === 'test') {
      return !this.state.registryAgentId();
    }
    if (key === 'compose') {
      return !this.state.rosterFullyStaffed() || this.state.composeProcessStatus() !== 'complete';
    }
    return false;
  });

  /**
   * What's missing for the disabled Compose forward step, shown as the
   * button's tooltip (spec §3, Stage 3: "disabled-button tooltip lists what's
   * missing"). `null` once the gate is satisfied (no tooltip needed).
   */
  readonly composeForwardDisabledReason = computed(() => {
    if (this.activeStageDef().key !== 'compose') return null;
    const missing: string[] = [];
    if (!this.state.rosterFullyStaffed()) missing.push('a fully-staffed roster');
    if (this.state.composeProcessStatus() !== 'complete') missing.push('a completed process');
    return missing.length > 0 ? `Needs: ${missing.join(' and ')}` : null;
  });

  /**
   * What's missing for the disabled Build forward step ("Test this agent →"),
   * shown as the button's tooltip (spec §3, Stage 1: "disabled ... with a
   * tooltip listing what's missing", same pattern as Compose). `draftAgentId`
   * distinguishes "nothing cloned yet" from "cloned but not saved";
   * `registryAgentId` is the final "saved" gate. `null` once satisfied.
   */
  readonly buildForwardDisabledReason = computed(() => {
    if (this.activeStageDef().key !== 'build') return null;
    if (!this.state.draftAgentId()) return 'Select or clone an agent to begin';
    if (!this.state.registryAgentId()) return 'Save the agent to continue';
    return null;
  });

  /** The active stage's forward-disabled tooltip text, whichever stage it is. */
  readonly forwardDisabledReason = computed(
    () => this.composeForwardDisabledReason() ?? this.buildForwardDisabledReason(),
  );

  /**
   * Temporary scaffold control: advance to the next stage. Real stages will
   * each own their forward gate (Build's and Stage 2's is `forwardDisabled`).
   */
  onContinue(): void {
    this.state.advance();
  }

  /**
   * Drives `unsavedChangesGuard` (route `canDeactivate`) so navigating away
   * from `/agent-studio` with unsaved handoff edits prompts Discard/Keep
   * editing, the same protection `/integrations`, `/user-profile`, and
   * `/llm-config` already have.
   *
   * Preconditions: none.
   * Postconditions: returns `state.isDirty()` — the same predicate that
   *   already gates the in-page load-conflict prompt.
   */
  hasUnsavedChanges(): boolean {
    return this.state.isDirty();
  }

  /**
   * Open the Save-draft popover (spec §3.5).
   *
   * Preconditions: none — always safe to call.
   * Postconditions: on a successful save, `state.currentDraftId()` /
   *   `currentDraftName()` reflect the saved draft and `markSaved` records
   *   the payload submitted at open (not response-time handoff) iff the
   *   session is still bound to the dialog's `draftId`; a load that rebound
   *   the session while the dialog was open leaves binding and snapshot
   *   unchanged. `isDirty()` is then false iff the current handoff still
   *   matches that payload. On cancel or failure, state is unchanged. The
   *   returned observable is the dialog's `afterClosed()`.
   */
  openSaveDraftDialog(): Observable<SaveDraftDialogResult | undefined> {
    const submitted = { ...this.state.handoff() };
    const capturedDraftId = this.state.currentDraftId();
    const data: SaveDraftDialogData = {
      draftId: capturedDraftId,
      initialName: this.state.currentDraftName(),
      payload: submitted,
    };
    const ref = this.dialog.open<SaveDraftDialogComponent, SaveDraftDialogData, SaveDraftDialogResult>(
      SaveDraftDialogComponent,
      // Backdrop click / Escape / browser Back-Forward must not bypass the
      // dialog's busy-guarded cancel(): dismissing while a create/update
      // request is in flight would leave the request to complete unobserved,
      // so a later Save would POST a duplicate draft instead of updating the
      // one that was actually created. `disableClose` only covers Escape/
      // backdrop — `closeOnNavigation` (Material default `true`) is a
      // separate flag that must be turned off too.
      //
      // `injector` is the shell's session injector so the overlay can resolve
      // `AgentStudioFacade` (provided here, not `root`). Without it, MatDialog
      // instantiates the dialog from the root injector and the façade inject
      // throws NullInjectorError.
      { data, width: '420px', disableClose: true, closeOnNavigation: false, injector: this.injector },
    );
    const closed$ = ref.afterClosed();
    closed$.subscribe((result) => {
      if (!result) return;
      if (this.state.currentDraftId() !== capturedDraftId) return;
      this.state.setCurrentDraft(result.draft_id, result.name);
      this.state.markSaved(submitted);
    });
    return closed$;
  }

  /**
   * Open the rename dialog for the bound draft.
   *
   * Preconditions: none — no-op when unbound or when the bound name is null.
   * Postconditions: on success, `currentDraftName()` matches the PATCH
   *   response iff the session is still bound to `draftId`; a load that
   *   rebound the session while the dialog was open leaves state unchanged.
   *   `isDirty()` is unchanged. On cancel/failure, state unchanged.
   */
  openRenameDraftDialog(): void {
    const draftId = this.state.currentDraftId();
    const initialName = this.state.currentDraftName();
    if (!draftId || initialName == null) return;
    const data: RenameDraftDialogData = { draftId, initialName };
    // `injector` is the shell's session injector so the overlay can resolve
    // `AgentStudioFacade` (provided here, not `root`) — see `openSaveDraftDialog`.
    // Without it, MatDialog instantiates the dialog from the root injector
    // and the façade inject throws NullInjectorError.
    const ref = this.dialog.open<RenameDraftDialogComponent, RenameDraftDialogData, RenameDraftDialogResult>(
      RenameDraftDialogComponent,
      { data, width: '420px', disableClose: true, closeOnNavigation: false, injector: this.injector },
    );
    ref.afterClosed().subscribe((result) => {
      if (!result) return;
      if (this.state.currentDraftId() !== draftId) return;
      this.state.setCurrentDraft(result.draft_id, result.name);
    });
  }

  /**
   * Handle a successful delete from the Load menu.
   *
   * Preconditions: `draftId` is a non-empty id.
   * Postconditions: if it was the bound draft, `currentDraftId()` /
   *   `currentDraftName()` are null, the saved snapshot is dropped so
   *   `isDirty()` is true while any handoff id remains, and handoff ids are
   *   unchanged. Otherwise state is unchanged.
   */
  onDraftDeleted(draftId: string): void {
    if (this.state.currentDraftId() === draftId) {
      this.state.setCurrentDraft(null, null);
      this.state.invalidateSavedSnapshot();
    }
  }

  /**
   * Load a saved draft and hydrate the session from it (spec §3.5 / §2.4).
   *
   * Preconditions: `draftId` names a draft the current user owns.
   * Postconditions: if `loadingDraft()` is already true, returns without
   *   starting any HTTP and state is unchanged. Otherwise, when `!isDirty()`,
   *   hydrates immediately. When dirty, opens the conflict dialog and
   *   `loadingDraft()` stays false until hydration HTTP starts. Cancel /
   *   Escape / backdrop: no HTTP, state unchanged. Discard: hydrate the
   *   chosen draft. Save first: persist current handoff then hydrate; a
   *   failed persist does not hydrate.
   */
  loadDraft(draftId: string): void {
    if (this.loadingDraft()) return;
    if (this.state.isDirty()) {
      this.resolveLoadConflict(draftId);
      return;
    }
    this.fetchAndHydrate(draftId);
  }

  /**
   * Open the unsaved-edit conflict dialog and route the user's choice.
   *
   * Preconditions: `state.isDirty()` is true; `draftId` is the draft the user
   *   asked to load.
   * Postconditions: `'discard'` starts `fetchAndHydrate`; `'save'` runs
   *   `saveFirstThenHydrate`; `undefined` (cancel) leaves state and HTTP
   *   unchanged. Does not set `loadingDraft()` — that begins only when
   *   hydration HTTP starts.
   */
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

  /**
   * Persist the current handoff, then hydrate the chosen draft.
   *
   * Preconditions: caller has already chosen `'save'` from the conflict
   *   dialog; `draftId` is the draft to load after a successful persist.
   * Postconditions: when bound (`currentDraftId` + `currentDraftName`), PUTs
   *   via `saveDraft` with `loadingDraft()` true for the whole PUT+hydrate.
   *   On success, records the submitted snapshot as saved and hydrates only
   *   if the session is still bound to `boundId` and the handoff still
   *   matches that snapshot. Concurrent edits, or a delete that unbound the
   *   draft while the PUT was in flight, stay dirty and are not overwritten.
   *   On PUT error, does not hydrate and
   *   `loadingDraft()` returns to false. When unbound, opens the Save-draft
   *   dialog; hydrates only if the session is still bound to the saved
   *   draft and the handoff still matches the submitted payload;
   *   empty/cancel/rebind aborts with no hydrate.
   */
  private saveFirstThenHydrate(draftId: string): void {
    const boundId = this.state.currentDraftId();
    const boundName = this.state.currentDraftName();
    if (boundId && boundName) {
      this.loadingDraft.set(true);
      const submitted = { ...this.state.handoff() };
      this.facade.saveDraft({ name: boundName, payload: submitted }, boundId).subscribe({
        next: (summary) => {
          if (this.state.currentDraftId() !== boundId) {
            this.loadingDraft.set(false);
            return;
          }
          this.state.setCurrentDraft(summary.draft_id, summary.name);
          this.state.markSaved(submitted);
          if (this.state.isDirty()) {
            this.loadingDraft.set(false);
            return;
          }
          this.fetchAndHydrate(draftId);
        },
        error: () => {
          this.loadingDraft.set(false);
          // Global HTTP interceptor toasts. Do not hydrate.
        },
      });
      return;
    }
    this.openSaveDraftDialog().subscribe((result) => {
      if (!result) return;
      if (this.state.currentDraftId() !== result.draft_id) return;
      if (this.state.isDirty()) return;
      this.fetchAndHydrate(draftId);
    });
  }

  /**
   * Fetch a draft and hydrate the session from it.
   *
   * Preconditions: `draftId` names a draft the current user owns; caller has
   *   already cleared any dirty-gate (clean session, discard, or successful
   *   save-first).
   * Postconditions: on success, `state` is hydrated from the draft's payload
   *   and the stepper moves to the furthest reachable stage.
   *   `loadingDraft()` stays `true` for the entire chain, including the
   *   nested process-status check. On failure, `loadingDraft()` returns to
   *   `false` and `state` is unchanged (surfaced via the global HTTP error
   *   toast, not a bespoke inline banner — the triggering menu has already
   *   closed by the time this runs). A call superseded by a later `loadDraft`
   *   (its token no longer matches `loadDraftToken`) discards its response
   *   instead of applying it. If the handoff IDs change while the GET is in
   *   flight, or a clean session becomes dirty (e.g. bound-draft delete
   *   invalidated the snapshot), hydration is aborted so that work is not
   *   overwritten.
   */
  private fetchAndHydrate(draftId: string): void {
    this.loadingDraft.set(true);
    const token = ++this.loadDraftToken;
    const captured = { ...this.state.handoff() };
    const wasDirty = this.state.isDirty();
    this.facade.loadDraft(draftId).subscribe({
      next: (draft) => {
        if (token !== this.loadDraftToken) return;
        if (!handoffEquals(this.state.handoff(), captured) || (!wasDirty && this.state.isDirty())) {
          this.loadingDraft.set(false);
          return;
        }
        this.hydrateFromDraft(draft, token);
      },
      error: () => {
        if (token !== this.loadDraftToken) return;
        this.loadingDraft.set(false);
      },
    });
  }

  /**
   * Replace the current session state with the contents of a loaded draft.
   *
   * Preconditions: caller has already run the token and dirty-state guards
   *   (see `fetchAndHydrate`).
   * Postconditions: `currentDraftId`/`currentDraftName` are bound to
   *   `draft`; the five handoff ids are set from `draft.payload`; the current
   *   persona live-run id is cleared (`state.setPersonaLiveRunId(null)`)
   *   because a persisted draft never contains an in-progress live run; the
   *   session is marked clean; the stepper advances to the furthest
   *   reachable stage via `resolveFurthestStage`.
   */
  private hydrateFromDraft(draft: AgentStudioDraft, token: number): void {
    this.state.setCurrentDraft(draft.draft_id, draft.name);
    const payload = draft.payload;
    this.state.setRegistryAgentId(asNullableString(payload['registryAgentId']));
    this.state.setTeamId(asNullableString(payload['teamId']));
    this.state.setProcessId(asNullableString(payload['processId']));
    this.state.setPersonaId(asNullableString(payload['personaId']));
    this.state.setDraftAgentId(asNullableString(payload['draftAgentId']));
    this.state.setPersonaLiveRunId(null);
    this.state.markClean();
    this.resolveFurthestStage(token);
  }

  /**
   * Move the stepper to the furthest stage the just-hydrated state supports
   * (spec §3.5's "furthest reachable stage" rule, restricted to what's
   * actually persisted today — no `stage1AgentDraft`, so Stage 1 always
   * resumes at its default "Start" sub-stage, explicitly reset here since the
   * sub-stepper isn't touched by navigating the main stepper alone and may
   * already be past Start from unrelated in-session Build progress).
   *
   * Deliberately does not call `setRosterFullyStaffed`: the spec is explicit
   * that Stage-4 reachability on load depends only on the process being
   * complete, not a fresh roster-staffed re-check ("the persisted
   * teamId/processId already passed the gate when saved, and Stage 4's own
   * safety net re-checks testability"). A stale `rosterFullyStaffed` from an
   * unrelated earlier session action is an accepted, spec-mandated tradeoff,
   * not an oversight.
   *
   * `token` guards the async `getProcess` branch against a superseded call's
   * late response — see `loadDraft`'s contract. Each branch finishes via
   * `finishSuccessfulDraftLoad`, which also returns the router to this
   * shell's default child if the persona-run audit was showing.
   */
  private resolveFurthestStage(token: number): void {
    const teamId = this.state.teamId();
    const processId = this.state.processId();
    if (teamId && processId) {
      this.agenticTeamApi.getProcess(processId).subscribe({
        next: (process) => {
          if (token !== this.loadDraftToken) return;
          this.state.setComposeProcessStatus(process.status);
          this.state.navigateToStage(process.status === 'complete' ? STAGE_INDEX.personas : STAGE_INDEX.compose);
          this.finishSuccessfulDraftLoad();
        },
        error: () => {
          if (token !== this.loadDraftToken) return;
          // The process may have been deleted/archived since the draft was
          // saved, or the lookup failed transiently — either way we no longer
          // know its status, so clear any stale value (e.g. 'complete' left
          // over from an earlier, unrelated in-session action) rather than
          // risk it wrongly satisfying the Stage-3→4 gate.
          this.state.setComposeProcessStatus(null);
          this.state.navigateToStage(STAGE_INDEX.compose);
          this.finishSuccessfulDraftLoad();
        },
      });
      return;
    }
    if (teamId) {
      this.state.navigateToStage(STAGE_INDEX.compose);
    } else if (this.state.registryAgentId()) {
      this.state.navigateToStage(STAGE_INDEX.test);
    } else {
      this.state.resetBuildSubStage();
      this.state.navigateToStage(STAGE_INDEX.build);
    }
    this.finishSuccessfulDraftLoad();
  }

  /**
   * Close out a draft load that already wrote `state` and the stepper.
   *
   * Preconditions: the caller's `loadDraftToken` still matches (superseded
   *   loads must not reach here).
   * Postconditions: if a non-default child is active, the router is asked to
   *   show this shell's default child; `loadingDraft()` is `false`.
   */
  private finishSuccessfulDraftLoad(): void {
    this.showStageHostAfterDraftLoad();
    this.loadingDraft.set(false);
  }

  /**
   * Leave a nested Studio child so the stage host can show the restored stage.
   *
   * Preconditions: none — safe when there is no child (unit tests that
   *   construct the shell without navigating).
   * Postconditions: if the active child path is non-empty (today:
   *   `persona-run/:runId`), navigates to this shell's default child. The
   *   empty default child and a missing child are left unchanged.
   */
  private showStageHostAfterDraftLoad(): void {
    const childPath = this.route.firstChild?.snapshot?.routeConfig?.path;
    if (!childPath) return;
    void this.router.navigate(['.'], { relativeTo: this.route });
  }
}
