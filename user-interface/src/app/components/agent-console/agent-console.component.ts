import { Component, signal, ViewChild } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatTabGroup, MatTabsModule } from '@angular/material/tabs';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { AgentCatalogComponent } from './agent-catalog/agent-catalog.component';
import { AgentRunnerComponent } from './agent-runner/agent-runner.component';
import { AgentProvisioningDashboardComponent } from '../agent-provisioning-dashboard/agent-provisioning-dashboard.component';

/**
 * Top-level page for the Agent Console.
 *
 * Hosts three tabs:
 *   - **Catalog** (default) — browse and inspect every registered agent.
 *   - **Runner** — invoke any agent in a per-team warm Docker sandbox.
 *   - **Provisioning & Environments** — embeds the existing provisioning
 *     dashboard verbatim so its behavior is unchanged.
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
