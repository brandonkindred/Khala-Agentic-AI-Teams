import { Component, inject } from '@angular/core';
import { Soc2ComplianceApiService } from '../../services/soc2-compliance-api.service';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import { DashboardShellComponent } from '../../shared/dashboard-shell/dashboard-shell.component';
import { NotificationService } from '../../core/notification.service';

@Component({
  selector: 'app-soc2-compliance-dashboard',
  standalone: true,
  imports: [
    DashboardShellComponent,
    MatButtonModule,
    MatIconModule,
    TeamAssistantChatComponent,
  ],
  templateUrl: './soc2-compliance-dashboard.component.html',
  styleUrl: './soc2-compliance-dashboard.component.scss',
})
export class Soc2ComplianceDashboardComponent {
  private readonly api = inject(Soc2ComplianceApiService);
  private readonly notifications = inject(NotificationService);

  healthCheck = (): ReturnType<Soc2ComplianceApiService['health']> =>
    this.api.health();

  /**
   * Confirm a launched audit with a transient snackbar (the app's convention for
   * successful actions), replacing the former persistent "queued" banner.
   *
   * Preconditions: `event.job_id` is the queued run id, or null when no run was created.
   * Postconditions: shows one "Audit queued" snackbar when a job id is present;
   * a null id is a no-op.
   */
  onWorkflowLaunched(event: { job_id: string | null; conversation_id: string }): void {
    if (event.job_id) {
      this.notifications.saved(`Audit queued — job ${event.job_id}.`);
    }
  }
}
