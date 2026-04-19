import { Component, inject } from '@angular/core';
import { MarketResearchApiService } from '../../services/market-research-api.service';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import { DashboardShellComponent } from '../../shared/dashboard-shell/dashboard-shell.component';

@Component({
  selector: 'app-market-research-dashboard',
  standalone: true,
  imports: [
    DashboardShellComponent,
    MatButtonModule,
    MatIconModule,
    TeamAssistantChatComponent,
  ],
  templateUrl: './market-research-dashboard.component.html',
  styleUrl: './market-research-dashboard.component.scss',
})
export class MarketResearchDashboardComponent {
  private readonly api = inject(MarketResearchApiService);

  /** Last-launched result payload — Market Research returns synchronously. */
  lastResult: Record<string, unknown> | null = null;

  healthCheck = (): ReturnType<MarketResearchApiService['health']> =>
    this.api.health();

  onWorkflowLaunched(event: { job_id: string | null; conversation_id: string }): void {
    // Market Research is synchronous; the upstream body carries the results.
    // `job_id` is always null for this team — documented in the assistant config.
    void event;
  }
}
