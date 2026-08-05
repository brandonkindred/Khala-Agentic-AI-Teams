import { Component, signal, ViewChild } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatTabGroup, MatTabsModule } from '@angular/material/tabs';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { AgentCatalogComponent } from './agent-catalog/agent-catalog.component';
import { AgentRunnerComponent } from './agent-runner/agent-runner.component';
import { AgentProvisioningDashboardComponent } from '../agent-provisioning-dashboard/agent-provisioning-dashboard.component';
import { BacklogTabComponent } from './backlog-tab/backlog-tab.component';
import { SprintsTabComponent } from './sprints-tab/sprints-tab.component';
import { FeedbackTabComponent } from './feedback-tab/feedback-tab.component';
import { CognitionTabComponent } from './cognition-tab/cognition-tab.component';
import { MetricsTabComponent } from './metrics-tab/metrics-tab.component';

/**
 * Top-level page for the Agent Console.
 *
 * Hosts eight tabs, in template order:
 *   - **Catalog** (default) — browse and inspect every registered agent.
 *   - **Runner** — invoke any agent in a per-team warm Docker sandbox.
 *   - **Provisioning & Environments** — embeds the existing provisioning
 *     dashboard verbatim so its behavior is unchanged.
 *   - **Backlog** — `product_delivery` initiatives/epics/stories with
 *     inline edit + grooming (#243 phase 4).
 *   - **Sprints** — sprint list with `Plan sprint` action (#243 phase 4).
 *   - **Feedback** — auto-promoted feedback with story-linking (#243
 *     phase 4 + the new PATCH /feedback/{id}/link route).
 *   - **Cognition** — operator surface for an agent's rule proposals
 *     (approve/reject), memory timeline, and rules.
 *   - **Metrics** — Software Engineering DORA metrics (deployment frequency,
 *     lead time, change-failure rate, MTTR) and LLM cost over a selectable window.
 */
@Component({
  selector: 'app-agent-console',
  standalone: true,
  imports: [
    CommonModule,
    MatTabsModule,
    MatIconModule,
    MatButtonModule,
    AgentCatalogComponent,
    AgentRunnerComponent,
    AgentProvisioningDashboardComponent,
    BacklogTabComponent,
    SprintsTabComponent,
    FeedbackTabComponent,
    CognitionTabComponent,
    MetricsTabComponent,
  ],
  templateUrl: './agent-console.component.html',
  styleUrl: './agent-console.component.scss',
})
export class AgentConsoleComponent {
  /** Agent id piped to the Runner tab from a Catalog drawer "Run" click. */
  readonly preselectedAgentId = signal<string | null>(null);

  @ViewChild(MatTabGroup) private tabGroup?: MatTabGroup;

  /** Emitted by the Catalog drawer. Switch to Runner and hand off the agent id. */
  onRunAgent(agentId: string): void {
    this.preselectedAgentId.set(agentId);
    if (this.tabGroup) {
      this.tabGroup.selectedIndex = 1; // Runner is the second tab.
    }
  }

  /** Emitted by Runner when user asks to go back to the catalog. */
  onReturnToCatalog(): void {
    if (this.tabGroup) {
      this.tabGroup.selectedIndex = 0;
    }
  }
}
