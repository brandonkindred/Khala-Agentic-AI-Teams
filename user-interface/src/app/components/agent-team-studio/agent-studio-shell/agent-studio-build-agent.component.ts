import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { A11yModule } from '@angular/cdk/a11y';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { AgentCatalogComponent } from '../agent-console/agent-catalog/agent-catalog.component';
import { AgentProvisionSlideOutComponent } from '../agent-provision-slide-out/agent-provision-slide-out.component';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';

/**
 * Agent Studio — Stage 1 "Build Agent" (spec §3, Stage 1). The entry point of
 * the journey: pick (or inspect) the registry agent to work on.
 *
 * Reuses the Agent Console catalog as-is (`app-agent-catalog`) for browse /
 * filter / inspect-drawer. Selecting an agent there (the drawer's run action)
 * records it as the journey's `registryAgentId`; the shell's gated
 * "Test this agent →" then advances to Stage 2 pre-seeded.
 *
 * Provisioning is folded into this stage (spec §3, Stage 1): a "Provision an
 * agent" affordance opens `AgentProvisionSlideOutComponent`, a thin,
 * route-agnostic wrapper (spec §4.1's Stage 1 adaptation caveat) around the
 * same provisioning chat used by the full `AgentProvisioningDashboardComponent`
 * — without that component's `DashboardShellComponent` page chrome or its
 * `ActivatedRoute`-based `?jobId=` deep link, neither of which apply on this
 * route. The full dashboard is untouched and still used as-is in Agent
 * Console's own "Provisioning & Environments" tab. The slide-out content is
 * self-contained, so it is only mounted while the panel is open. The
 * slide-out itself is a proper modal: a CDK focus trap with auto-capture
 * moves focus into the panel, keeps Tab cycling inside it, and restores focus
 * to the trigger on close; Escape dismisses it.
 */
@Component({
  selector: 'app-agent-studio-build-agent',
  standalone: true,
  imports: [
    A11yModule,
    MatButtonModule,
    MatIconModule,
    AgentCatalogComponent,
    AgentProvisionSlideOutComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './agent-studio-build-agent.component.html',
  styleUrl: './agent-studio-build-agent.component.scss',
})
export class AgentStudioBuildAgentComponent {
  private readonly state = inject(AgentStudioStateService);

  /** The agent picked for the journey (null until one is selected). */
  readonly selectedAgentId = computed(() => this.state.registryAgentId());

  /** Whether the provisioning slide-out is open. */
  readonly provisionOpen = signal(false);

  /**
   * Record the catalog's selected agent as the journey's `registryAgentId`.
   * This is the "select" step only — advancing to Stage 2 is the shell's gated
   * "Test this agent →" affordance (spec §3, Stage 1 handoff).
   */
  onSelectAgent(agentId: string): void {
    this.state.setRegistryAgentId(agentId);
  }

  openProvision(): void {
    this.provisionOpen.set(true);
  }

  closeProvision(): void {
    this.provisionOpen.set(false);
  }
}
