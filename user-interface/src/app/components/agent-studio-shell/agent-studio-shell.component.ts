import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { STUDIO_STAGES } from '../../models/agent-studio.model';
import { AgentStudioStateService } from '../../services/agent-studio-state.service';
import { AgentStudioStagePlaceholderComponent } from './agent-studio-stage-placeholder.component';
import { AgentStudioTestAgentComponent } from './agent-studio-test-agent.component';

/**
 * Agent Studio shell — the single `/agent-studio` surface (spec §2.1). Renders
 * the forward-only 4-stage stepper and the active stage. Stage 2 (Test Agent)
 * is implemented; Build / Compose / Personas are still stubbed by the
 * placeholder. Owns one `AgentStudioStateService` per session (provided here,
 * not at root, so each visit starts clean).
 */
@Component({
  selector: 'app-agent-studio-shell',
  standalone: true,
  imports: [
    MatButtonModule,
    MatIconModule,
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
   * Stage 2 ("Test Agent") gates "Add to team →" on an agent actually being
   * selected, so the journey can't hand an empty roster candidate to Stage 3.
   * Other stages keep the scaffold's always-enabled forward step until their
   * own gates land.
   */
  readonly forwardDisabled = computed(
    () => this.activeStageDef().key === 'test' && !this.state.registryAgentId(),
  );

  /**
   * Temporary scaffold control: advance to the next stage. Real stages will
   * each own their forward gate (Stage 2's is `forwardDisabled` above).
   */
  onContinue(): void {
    this.state.advance();
  }
}
