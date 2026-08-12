import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatDialog, MatDialogModule } from '@angular/material/dialog';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { STUDIO_STAGES } from '../../../models/agent-studio.model';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { AgentStudioBuildAgentComponent } from './agent-studio-build-agent.component';
import { AgentStudioComposeTeamComponent } from './agent-studio-compose-team.component';
import { AgentStudioPersonaComponent } from './agent-studio-persona.component';
import { AgentStudioStagePlaceholderComponent } from './agent-studio-stage-placeholder.component';
import { AgentStudioTestAgentComponent } from './agent-studio-test-agent.component';
import {
  SaveDraftDialogComponent,
  type SaveDraftDialogData,
  type SaveDraftDialogResult,
} from './save-draft-dialog/save-draft-dialog.component';

/**
 * Agent Studio shell — the single `/agent-studio` surface (spec §2.1). Renders
 * the forward-only 4-stage stepper and the active stage. All four stages are
 * implemented: Build Agent, Test Agent, Compose Team, and Test Team w/
 * Personas. Owns one `AgentStudioStateService` per session (provided here,
 * not at root, so each visit starts clean).
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
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [AgentStudioStateService],
  templateUrl: './agent-studio-shell.component.html',
  styleUrl: './agent-studio-shell.component.scss',
})
export class AgentStudioShellComponent {
  readonly state = inject(AgentStudioStateService);
  private readonly dialog = inject(MatDialog);
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
   * Open the Save-draft popover (spec §3.5). Assembles the payload from the
   * currently-available handoff state and lets the dialog decide create vs
   * update based on whether this session is already bound to a server draft.
   *
   * Preconditions: none — always safe to call.
   * Postconditions: on a successful save, `state.currentDraftId()`/
   *   `currentDraftName()` reflect the saved draft. On cancel or failure,
   *   state is unchanged.
   */
  openSaveDraftDialog(): void {
    const data: SaveDraftDialogData = {
      draftId: this.state.currentDraftId(),
      initialName: this.state.currentDraftName(),
      payload: { ...this.state.handoff() },
    };
    const ref = this.dialog.open<SaveDraftDialogComponent, SaveDraftDialogData, SaveDraftDialogResult>(
      SaveDraftDialogComponent,
      // Backdrop click / Escape must not bypass the dialog's busy-guarded
      // cancel(): dismissing while a create/update request is in flight would
      // leave the request to complete unobserved, so a later Save would POST
      // a duplicate draft instead of updating the one that was actually created.
      { data, width: '420px', disableClose: true },
    );
    ref.afterClosed().subscribe((result) => {
      if (!result) return;
      this.state.setCurrentDraft(result.draft_id, result.name);
    });
  }
}
