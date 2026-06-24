import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { STUDIO_STAGES } from '../../models/agent-studio.model';
import { AgentStudioStateService } from '../../services/agent-studio-state.service';
import { AgentStudioStagePlaceholderComponent } from './agent-studio-stage-placeholder.component';

/**
 * Agent Studio shell — the single `/agent-studio` surface (spec §2.1). Renders
 * the forward-only 4-stage stepper and the active stage. Stages are stubbed in
 * this scaffold. Owns one `AgentStudioStateService` per session (provided here,
 * not at root, so each visit starts clean).
 */
@Component({
  selector: 'app-agent-studio-shell',
  standalone: true,
  imports: [MatButtonModule, MatIconModule, AgentStudioStagePlaceholderComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [AgentStudioStateService],
  templateUrl: './agent-studio-shell.component.html',
  styleUrl: './agent-studio-shell.component.scss',
})
export class AgentStudioShellComponent {
  readonly state = inject(AgentStudioStateService);
  /** The forward-only stage list rendered by the stepper. */
  readonly stages = STUDIO_STAGES;

  /** Descriptor of the stage currently shown (drives the placeholder). */
  readonly activeStageDef = computed(() => this.stages[this.state.activeStage()]);

  /**
   * Temporary scaffold control: advance to the next stage. Real stages will
   * each own their forward affordance (e.g. "Test this agent →").
   */
  onContinue(): void {
    this.state.advance();
  }
}
