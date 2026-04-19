import { Component } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import { DashboardShellComponent } from '../../shared/dashboard-shell/dashboard-shell.component';

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
  latestJobId: string | null = null;

  onWorkflowLaunched(event: { job_id: string | null; conversation_id: string }): void {
    this.latestJobId = event.job_id;
  }
}
