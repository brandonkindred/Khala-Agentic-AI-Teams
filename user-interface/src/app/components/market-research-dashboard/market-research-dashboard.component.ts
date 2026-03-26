import { Component, inject } from '@angular/core';
import { MarketResearchApiService } from '../../services/market-research-api.service';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { MarketResearchFormComponent } from '../market-research-form/market-research-form.component';
import { MarketResearchResultsComponent } from '../market-research-results/market-research-results.component';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import type { RunMarketResearchRequest, TeamOutput } from '../../models';

@Component({
  selector: 'app-market-research-dashboard',
  standalone: true,
  imports: [
    LoadingSpinnerComponent,
    ErrorMessageComponent,
    HealthIndicatorComponent,
    MarketResearchFormComponent,
    MarketResearchResultsComponent,
    MatButtonModule,
    MatIconModule,
    TeamAssistantChatComponent,
  ],
  templateUrl: './market-research-dashboard.component.html',
  styleUrl: './market-research-dashboard.component.scss',
})
export class MarketResearchDashboardComponent {
  private readonly api = inject(MarketResearchApiService);

  mode: 'chat' | 'form' = 'chat';
  loading = false;
  error: string | null = null;
  result: TeamOutput | null = null;

  healthCheck = (): ReturnType<MarketResearchApiService['health']> =>
    this.api.health();

  onSubmit(request: RunMarketResearchRequest): void {
    this.loading = true;
    this.error = null;
    this.result = null;
    this.api.run(request).subscribe({
      next: (res) => {
        this.result = res;
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Request failed';
        this.loading = false;
      },
    });
  }
}
