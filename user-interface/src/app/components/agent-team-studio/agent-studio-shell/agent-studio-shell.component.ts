import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatDialog, MatDialogModule } from '@angular/material/dialog';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { Observable } from 'rxjs';
import type { AgentStudioDraft } from '../../../models/agent-studio.model';
import { STUDIO_STAGES } from '../../../models/agent-studio.model';
import { AgentStudioApiService } from '../../../services/agent-studio-api.service';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { AgentStudioFacade } from '../../../services/agent-studio.facade';
import { AgenticTeamApiService } from '../../../services/agentic-team-api.service';
import { AgentStudioBuildAgentComponent } from './agent-studio-build-agent.component';
import { AgentStudioComposeTeamComponent } from './agent-studio-compose-team.component';
import { AgentStudioPersonaComponent } from './agent-studio-persona.component';
import { AgentStudioStagePlaceholderComponent } from './agent-studio-stage-placeholder.component';
import { AgentStudioTestAgentComponent } from './agent-studio-test-agent.component';
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

/** Stage indices for `navigateToStage` (mirrors `agent-studio-persona.component.ts`'s convention). */
const STAGE_BUILD = 0;
const STAGE_TEST = 1;
const STAGE_COMPOSE = 2;
const STAGE_PERSONAS = 3;

/** `draft.payload` is an opaque, backend-unvalidated blob (spec §3.5) — never
 *  trust a field's type without checking it first. */
function asNullableString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

/**
 * Agent Studio shell — the single `/agent-studio` surface (spec §2.1). Renders
 * the forward-only 4-stage stepper and the active stage. All four stages are
 * implemented: Build Agent, Test Agent, Compose Team, and Test Team w/
 * Personas. Owns one `AgentStudioStateService` per session (provided here,
 * not at root, so each visit starts clean). `AgentStudioFacade` is provided
 * alongside it so the facade's injector can resolve this session's state
 * service (it is not a root singleton either).
 */
@Component({
  selector: 'app-agent-studio-shell',
  standalone: true,
  imports: [
    MatButtonModule,
    MatDialogModule,
    MatIconModule,
    MatTooltipModule,
    AgentStudioBuildAgentComponent,
    AgentStudioComposeTeamComponent,
    AgentStudioPersonaComponent,
    AgentStudioStagePlaceholderComponent,
    AgentStudioTestAgentComponent,
    LoadDraftMenuComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [AgentStudioStateService, AgentStudioFacade],
  templateUrl: './agent-studio-shell.component.html',
  styleUrl: './agent-studio-shell.component.scss',
})
export class AgentStudioShellComponent {
  readonly state = inject(AgentStudioStateService);
  private readonly dialog = inject(MatDialog);
  private readonly api = inject(AgentStudioApiService);
  private readonly agenticTeamApi = inject(AgenticTeamApiService);

  /** True while a Load-draft selection is being fetched and hydrated. */
  readonly loadingDraft = signal(false);
  /** Bumped on every `loadDraft` call; lets a superseded call's late-arriving
   *  responses (`getDraft`, the nested `getProcess` check) recognize they're
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
      // Backdrop click / Escape / browser Back-Forward must not bypass the
      // dialog's busy-guarded cancel(): dismissing while a create/update
      // request is in flight would leave the request to complete unobserved,
      // so a later Save would POST a duplicate draft instead of updating the
      // one that was actually created. `disableClose` only covers Escape/
      // backdrop — `closeOnNavigation` (Material default `true`) is a
      // separate flag that must be turned off too.
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
   *   via `updateDraft`, `markClean`s on success, then hydrates. On PUT
   *   error, does not hydrate. When unbound, opens the Save-draft dialog;
   *   truthy result hydrates, empty/cancel aborts with no hydrate.
   */
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
   *   `false` and `state` is unchanged. A call superseded by a later
   *   `loadDraft` (its token no longer matches `loadDraftToken`) discards
   *   its response instead of applying it.
   */
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

  private hydrateFromDraft(draft: AgentStudioDraft, token: number): void {
    this.state.setCurrentDraft(draft.draft_id, draft.name);
    const payload = draft.payload;
    this.state.setRegistryAgentId(asNullableString(payload['registryAgentId']));
    this.state.setTeamId(asNullableString(payload['teamId']));
    this.state.setProcessId(asNullableString(payload['processId']));
    this.state.setPersonaId(asNullableString(payload['personaId']));
    this.state.setDraftAgentId(asNullableString(payload['draftAgentId']));
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
   * late response — see `loadDraft`'s contract.
   */
  private resolveFurthestStage(token: number): void {
    const teamId = this.state.teamId();
    const processId = this.state.processId();
    if (teamId && processId) {
      this.agenticTeamApi.getProcess(processId).subscribe({
        next: (process) => {
          if (token !== this.loadDraftToken) return;
          this.state.setComposeProcessStatus(process.status);
          this.state.navigateToStage(process.status === 'complete' ? STAGE_PERSONAS : STAGE_COMPOSE);
          this.loadingDraft.set(false);
        },
        error: () => {
          if (token !== this.loadDraftToken) return;
          // The process may have been deleted/archived since the draft was
          // saved, or the lookup failed transiently — either way we no longer
          // know its status, so clear any stale value (e.g. 'complete' left
          // over from an earlier, unrelated in-session action) rather than
          // risk it wrongly satisfying the Stage-3→4 gate.
          this.state.setComposeProcessStatus(null);
          this.state.navigateToStage(STAGE_COMPOSE);
          this.loadingDraft.set(false);
        },
      });
      return;
    }
    if (teamId) {
      this.state.navigateToStage(STAGE_COMPOSE);
    } else if (this.state.registryAgentId()) {
      this.state.navigateToStage(STAGE_TEST);
    } else {
      this.state.resetBuildSubStage();
      this.state.navigateToStage(STAGE_BUILD);
    }
    this.loadingDraft.set(false);
  }
}
