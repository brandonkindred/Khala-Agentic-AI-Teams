import { Component, inject } from '@angular/core';
import { Soc2ComplianceApiService } from '../../services/soc2-compliance-api.service';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import { DashboardShellComponent } from '../../shared/dashboard-shell/dashboard-shell.component';

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

  healthCheck = (): ReturnType<Soc2ComplianceApiService['health']> =>
    this.api.health();
}
