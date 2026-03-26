import { Component, inject } from '@angular/core';
import { Soc2ComplianceApiService } from '../../services/soc2-compliance-api.service';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { Soc2AuditFormComponent } from '../soc2-audit-form/soc2-audit-form.component';
import { Soc2AuditStatusComponent } from '../soc2-audit-status/soc2-audit-status.component';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import type { RunAuditRequest } from '../../models';

@Component({
  selector: 'app-soc2-compliance-dashboard',
  standalone: true,
  imports: [
    LoadingSpinnerComponent,
    ErrorMessageComponent,
    HealthIndicatorComponent,
    Soc2AuditFormComponent,
    Soc2AuditStatusComponent,
    MatButtonModule,
    MatIconModule,
    TeamAssistantChatComponent,
  ],
  templateUrl: './soc2-compliance-dashboard.component.html',
  styleUrl: './soc2-compliance-dashboard.component.scss',
})
export class Soc2ComplianceDashboardComponent {
  private readonly api = inject(Soc2ComplianceApiService);

  mode: 'chat' | 'form' = 'chat';
  loading = false;
  error: string | null = null;
  jobId: string | null = null;

  healthCheck = (): ReturnType<Soc2ComplianceApiService['health']> =>
    this.api.health();

  onSubmit(request: RunAuditRequest): void {
    this.loading = true;
    this.error = null;
    this.jobId = null;
    this.api.runAudit(request).subscribe({
      next: (res) => {
        this.jobId = res.job_id;
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Request failed';
        this.loading = false;
      },
    });
  }
}
