import { Component, inject } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import { DashboardShellComponent } from '../../shared/dashboard-shell/dashboard-shell.component';
import { NotificationService } from '../../core/notification.service';

@Component({
  selector: 'app-social-marketing-dashboard',
  standalone: true,
  imports: [
    MatButtonModule,
    MatIconModule,
    TeamAssistantChatComponent,
    DashboardShellComponent,
  ],
  templateUrl: './social-marketing-dashboard.component.html',
  styleUrl: './social-marketing-dashboard.component.scss',
})
export class SocialMarketingDashboardComponent {
  private readonly notifications = inject(NotificationService);

  /**
   * Confirm a launched workflow with a transient snackbar (the app's convention
   * for successful actions), replacing the former persistent "queued" banner.
   *
   * Preconditions: `event.job_id` is the queued run id, or null when no run was created.
   * Postconditions: shows one "Campaign queued" snackbar when a job id is present;
   * a null id is a no-op.
   */
  onWorkflowLaunched(event: { job_id: string | null; conversation_id: string }): void {
    if (event.job_id) {
      this.notifications.saved(`Campaign queued — job ${event.job_id}.`);
    }
  }
}
