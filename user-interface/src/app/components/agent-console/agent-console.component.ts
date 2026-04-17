import { Component } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatTabsModule } from '@angular/material/tabs';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { AgentCatalogComponent } from './agent-catalog/agent-catalog.component';
import { AgentProvisioningDashboardComponent } from '../agent-provisioning-dashboard/agent-provisioning-dashboard.component';

/**
 * Top-level page for the Agent Console.
 *
 * Hosts three tabs:
 *   - **Catalog** (default) — browse and inspect every registered agent.
 *   - **Runner** — placeholder; isolated agent invocation ships in Phase 2.
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
    AgentProvisioningDashboardComponent,
  ],
  templateUrl: './agent-console.component.html',
  styleUrl: './agent-console.component.scss',
})
export class AgentConsoleComponent {}
