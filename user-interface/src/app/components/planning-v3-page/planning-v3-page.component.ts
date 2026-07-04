import { Component, inject } from '@angular/core';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { PlanningV3ApiService } from '../../services/planning-v3-api.service';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import { NotificationService } from '../../core/notification.service';

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
  private readonly notifications = inject(NotificationService);

  healthCheck = (): ReturnType<PlanningV3ApiService['health']> =>
    this.api.health();

  /**
   * Confirm a launched planning job with a transient snackbar (the app's
   * convention for successful actions), replacing the former persistent banner.
   *
   * Preconditions: `event.job_id` is the queued run id, or null when no run was created.
   * Postconditions: shows one "Planning job queued" snackbar when a job id is
   * present; a null id is a no-op.
   */
  onWorkflowLaunched(event: { job_id: string | null; conversation_id: string }): void {
    if (event.job_id) {
      this.notifications.saved(`Planning job queued — id ${event.job_id}.`);
    }
  }
}
