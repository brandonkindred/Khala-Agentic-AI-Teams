import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { AgentCatalogComponent } from '../agent-console/agent-catalog/agent-catalog.component';
import { AgentRunnerComponent } from '../agent-console/agent-runner/agent-runner.component';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { AgentStudioSlideOutComponent } from './agent-studio-slide-out/agent-studio-slide-out.component';
import { STAGE_INDEX } from '../../../models/agent-studio.model';

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
 * **Browse agents (spec §2.1).** The forward-only stepper never jumps back to
 * Stage 1, so picking a *different* agent once here is an explicit in-context
 * action: a `[ Browse agents ]` overlay hosts the catalog again, using the
 * shared `AgentStudioSlideOutComponent` (the same scrim + focus-trapped panel
 * chrome as Stage 1's provisioning panel). Selecting an agent there just
 * re-points `registryAgentId` — since `AgentRunnerComponent` exposes
 * `preselectedAgentId` as an `@Input()` setter, reassigning it already resets
 * run history and re-warms the sandbox for the new agent.
 */
@Component({
  selector: 'app-agent-studio-test-agent',
  standalone: true,
  imports: [MatButtonModule, MatIconModule, AgentCatalogComponent, AgentRunnerComponent, AgentStudioSlideOutComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './agent-studio-test-agent.component.html',
  styleUrl: './agent-studio-test-agent.component.scss',
})
export class AgentStudioTestAgentComponent {
  private readonly state = inject(AgentStudioStateService);

  /** The agent to test, carried from Stage 1 (null until one is selected). */
  readonly agentId = computed(() => this.state.registryAgentId());

  /** Whether the "Browse agents" overlay is open. */
  readonly browseOpen = signal(false);

  /**
   * Return to Stage 1 (Build). Wired to the runner's "back to catalog"
   * affordance and the empty-state button: in the Studio the catalog *is*
   * Stage 1, so returning to it means moving the stepper back to Build.
   */
  onReturnToBuild(): void {
    this.state.navigateToStage(STAGE_INDEX.build);
  }

  openBrowse(): void {
    this.browseOpen.set(true);
  }

  closeBrowse(): void {
    this.browseOpen.set(false);
  }

  /**
   * Re-point the handoff agent to `id` from the Browse-agents overlay (spec
   * §2.1) — not a clone, just a focus change. `AgentRunnerComponent` reacts to
   * the resulting `preselectedAgentId` change on its own (fresh run history,
   * re-warmed sandbox), so no further reset is needed here.
   */
  onBrowseSelect(id: string): void {
    this.state.setRegistryAgentId(id);
    this.closeBrowse();
  }
}
