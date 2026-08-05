import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { AgentRunnerComponent } from '../agent-console/agent-runner/agent-runner.component';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';

/**
 * Agent Studio — Stage 2 "Test Agent" (spec §3, Stage 2).
 *
 * Runs the agent chosen in Stage 1 inside its sandbox by reusing the Agent
 * Console runner as-is (`app-agent-runner`), pre-seeded from the handoff
 * `registryAgentId`. The runner owns all sandbox / invoke / saved-input /
 * run-history / diff behaviour; this stage only seeds it and frames the agent.
 * The forward "Add to team →" affordance lives in the shell footer (the shell
 * owns the guided forward step), so this stage adds no second forward button.
 *
 * When no agent is selected yet (the stage was reached without a Stage-1
 * selection), it renders an empty state rather than an unseeded runner — so the
 * heavy runner only mounts once there is a real agent to run — with a
 * contextual jump back to Build.
 */
@Component({
  selector: 'app-agent-studio-test-agent',
  standalone: true,
  imports: [MatButtonModule, MatIconModule, AgentRunnerComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './agent-studio-test-agent.component.html',
  styleUrl: './agent-studio-test-agent.component.scss',
})
export class AgentStudioTestAgentComponent {
  private readonly state = inject(AgentStudioStateService);

  /** The agent to test, carried from Stage 1 (null until one is selected). */
  readonly agentId = computed(() => this.state.registryAgentId());

  /**
   * Return to Stage 1 (Build). Wired to the runner's "back to catalog"
   * affordance and the empty-state button: in the Studio the catalog *is*
   * Stage 1, so returning to it means moving the stepper back to Build.
   */
  onReturnToBuild(): void {
    this.state.navigateToStage(0);
  }
}
