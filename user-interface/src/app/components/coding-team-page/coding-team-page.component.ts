import { Component, inject } from '@angular/core';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { RouterLink } from '@angular/router';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';

@Component({
  selector: 'app-coding-team-page',
  standalone: true,
  imports: [
    MatIconModule,
    MatButtonModule,
    RouterLink,
    HealthIndicatorComponent,
    TeamAssistantChatComponent,
  ],
  templateUrl: './coding-team-page.component.html',
  styleUrl: './coding-team-page.component.scss',
})
export class CodingTeamPageComponent {
  private readonly api = inject(CodingTeamApiService);

  latestJobId: string | null = null;

  healthCheck = (): ReturnType<CodingTeamApiService['health']> => this.api.health();

  onWorkflowLaunched(event: { job_id: string | null; conversation_id: string }): void {
    this.latestJobId = event.job_id;
  }
}
