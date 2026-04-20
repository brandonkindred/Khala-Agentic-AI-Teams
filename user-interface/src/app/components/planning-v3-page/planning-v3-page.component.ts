import { Component, inject } from '@angular/core';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { PlanningV3ApiService } from '../../services/planning-v3-api.service';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';

@Component({
  selector: 'app-planning-v3-page',
  standalone: true,
  imports: [
    MatIconModule,
    MatButtonModule,
    HealthIndicatorComponent,
    TeamAssistantChatComponent,
  ],
  templateUrl: './planning-v3-page.component.html',
  styleUrl: './planning-v3-page.component.scss',
})
export class PlanningV3PageComponent {
  private readonly api = inject(PlanningV3ApiService);

  latestJobId: string | null = null;

  healthCheck = (): ReturnType<PlanningV3ApiService['health']> =>
    this.api.health();

  onWorkflowLaunched(event: { job_id: string | null; conversation_id: string }): void {
    this.latestJobId = event.job_id;
  }
}
