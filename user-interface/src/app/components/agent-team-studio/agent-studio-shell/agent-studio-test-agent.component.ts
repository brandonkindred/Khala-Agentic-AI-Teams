import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { AgentCatalogComponent } from '../agent-console/agent-catalog/agent-catalog.component';
import { AgentRunnerComponent } from '../agent-console/agent-runner/agent-runner.component';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { AgentStudioSlideOutComponent } from './agent-studio-slide-out/agent-studio-slide-out.component';

/**
 * Agent Studio — Stage 2 "Test Agent" (spec §3, Stage 2).
 *
 * Runs the agent chosen in Stage 1 inside its sandbox by reusing the Agent
 * Console runner as-is (`app-agent-runner`), pre-seeded from the handoff
 * `registryAgentId`. The runner owns all sandbox / invoke / saved-input /
 * run-history / diff behaviour (including `AgentRunnerApiService`); this
 * stage only seeds it and frames the agent. Stage 2's happy path therefore
 * has no Studio HTTP-client injection of its own — invoke/sandbox stay on
 * the reused Console runner rather than being re-wired through
 * `AgentStudioFacade` (those façade methods exist for Studio-owned callers;
 * this stage is not one). The forward "Add to team →" affordance lives in
 * the shell footer (the shell owns the guided forward step), so this stage
 * adds no second forward button.
 *
 * When no agent is selected yet (the stage was reached without a Stage-1
 * selection), it renders an empty state rather than an unseeded runner — so the
 * heavy runner only mounts once there is a real agent to run — with a
 * contextual jump back to Build.
 *
 * A "Browse" affordance (spec §3, Stage 2) reuses the Agent Console catalog
 * (`app-agent-catalog`) in the shared `AgentStudioSlideOutComponent` overlay
 * to let the user switch which agent is under test without leaving Stage 2.
 */
@Component({
  selector: 'app-agent-studio-test-agent',
  standalone: true,
  imports: [MatButtonModule, MatIconModule, AgentRunnerComponent, AgentCatalogComponent, AgentStudioSlideOutComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './agent-studio-test-agent.component.html',
  styleUrl: './agent-studio-test-agent.component.scss',
})
export class AgentStudioTestAgentComponent {
  private readonly state = inject(AgentStudioStateService);

  /** The agent to test, carried from Stage 1 (null until one is selected). */
  readonly agentId = computed(() => this.state.registryAgentId());

  /** Whether the Browse-agents overlay is open. */
  readonly browseOpen = signal(false);

  /**
   * Return to Stage 1 (Build). Wired to the runner's "back to catalog"
   * affordance and the empty-state button: in the Studio the catalog *is*
   * Stage 1, so returning to it means moving the stepper back to Build.
   */
  onReturnToBuild(): void {
    this.state.navigateToStage(0);
  }

  /**
   * Open the Browse-agents overlay.
   *
   * Preconditions: none — always safe to call.
   * Postconditions: `browseOpen()` is true.
   */
  openBrowse(): void {
    this.browseOpen.set(true);
  }

  /**
   * Close the Browse-agents overlay without changing the tested agent.
   *
   * Preconditions: none.
   * Postconditions: `browseOpen()` is false.
   */
  closeBrowse(): void {
    this.browseOpen.set(false);
  }

  /**
   * Handle a catalog selection from the Browse overlay: switch the agent
   * under test and dismiss the overlay.
   *
   * Preconditions: `agentId` is a non-empty registry agent id emitted by the
   *   catalog's `requestRun` output.
   * Postconditions: `state.registryAgentId()` equals `agentId`; `browseOpen()`
   *   is false. `AgentRunnerComponent`'s `[preselectedAgentId]` input re-seeds
   *   on the next change-detection pass because `agentId()` (bound to it)
   *   changes.
   */
  onBrowseSelect(agentId: string): void {
    this.state.setRegistryAgentId(agentId);
    this.closeBrowse();
  }
}
