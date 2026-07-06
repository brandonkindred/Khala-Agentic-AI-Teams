import { Component, inject } from '@angular/core';
import { PlanningApiService } from '../../services/planning-api.service';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import { NotificationService } from '../../core/notification.service';
import { environment } from '../../../environments/environment';

/** Page component for the Planning team dashboard: health status, the run form, job status, and the team-assistant chat. */
@Component({
  selector: 'app-planning-page',
  standalone: true,
  imports: [
    HealthIndicatorComponent,
    TeamAssistantChatComponent,
  ],
  templateUrl: './planning-page.component.html',
  styleUrl: './planning-page.component.scss',
})
export class PlanningPageComponent {
  private readonly api = inject(PlanningApiService);
  private readonly notifications = inject(NotificationService);

  /** Assistant endpoint for the team-assistant chat, derived from the configured Planning API base URL. */
  readonly teamApiUrl = `${environment.planningApiUrl}/assistant`;

  healthCheck = (): ReturnType<PlanningApiService['health']> =>
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
