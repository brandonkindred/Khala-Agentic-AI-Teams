import { Component, inject, OnDestroy, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatTabsModule } from '@angular/material/tabs';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatChipsModule } from '@angular/material/chips';
import { Observable, Subscription } from 'rxjs';
import { AccessibilityApiService } from '../../services/accessibility-api.service';
import { DashboardShellComponent } from '../../shared/dashboard-shell/dashboard-shell.component';
import { AccessibilityAuditFormComponent } from '../accessibility-audit-form/accessibility-audit-form.component';
import { AccessibilityJobStatusComponent } from '../accessibility-job-status/accessibility-job-status.component';
import { AccessibilityFindingsComponent } from '../accessibility-findings/accessibility-findings.component';
import { AccessibilityReportComponent } from '../accessibility-report/accessibility-report.component';
import { AccessibilityDesignSystemComponent } from '../accessibility-design-system/accessibility-design-system.component';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import type {
  CreateAuditRequest,
  AuditJobResponse,
  AccessibilityAuditStatusResponse,
  HealthResponse,
  RetestRequest,
} from '../../models';

type DashboardTab = 'create' | 'status' | 'findings' | 'report' | 'design-system';

@Component({
  selector: 'app-accessibility-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    MatTabsModule,
    MatIconModule,
    MatButtonModule,
    MatChipsModule,
    AccessibilityAuditFormComponent,
    AccessibilityJobStatusComponent,
    AccessibilityFindingsComponent,
    AccessibilityReportComponent,
    AccessibilityDesignSystemComponent,
    TeamAssistantChatComponent,
    DashboardShellComponent,
  ],
  templateUrl: './accessibility-dashboard.component.html',
  styleUrl: './accessibility-dashboard.component.scss',
})
export class AccessibilityDashboardComponent implements OnInit, OnDestroy {
  private readonly api = inject(AccessibilityApiService);
  private healthSub: Subscription | null = null;

  healthCheckFn: () => Observable<{ status?: string }> = () => this.api.healthCheck();

  selectedTabIndex = 0;
  activeTab: DashboardTab = 'create';

  healthStatus: HealthResponse | null = null;
  healthLoading = false;
  healthError: string | null = null;

  jobId: string | null = null;
  auditId: string | null = null;

  lastJobResponse: AuditJobResponse | null = null;
  lastStatus: AccessibilityAuditStatusResponse | null = null;

  retestLoading = false;
  retestError: string | null = null;

  ngOnInit(): void {
    this.checkHealth();
  }

  ngOnDestroy(): void {
    this.healthSub?.unsubscribe();
  }

  checkHealth(): void {
    this.healthLoading = true;
    this.healthError = null;

    this.healthSub = this.api.healthCheck().subscribe({
      next: (res) => {
        this.healthStatus = res;
        this.healthLoading = false;
      },
      error: (err) => {
        this.healthError = err?.message ?? 'API unavailable';
        this.healthLoading = false;
      },
    });
  }

  onTabChange(index: number): void {
    this.selectedTabIndex = index;
    const tabs: DashboardTab[] = ['create', 'status', 'findings', 'report', 'design-system'];
    this.activeTab = tabs[index] || 'create';
  }

  onAuditSubmit(request: CreateAuditRequest): void {
    this.api.createAudit(request).subscribe({
      next: (res: AuditJobResponse) => {
        this.lastJobResponse = res;
        this.jobId = res.job_id;
        this.auditId = res.audit_id;
        this.selectedTabIndex = 1;
        this.activeTab = 'status';
      },
      error: (err) => {
        console.error('Failed to create audit:', err);
      },
    });
  }

  /** Handle a launch triggered by the shared team assistant chat. */
  onAssistantLaunched(event: { job_id: string | null; conversation_id: string }): void {
    if (event.job_id) {
      this.jobId = event.job_id;
      this.selectedTabIndex = 1;
      this.activeTab = 'status';
    }
  }

  onStatusChange(status: AccessibilityAuditStatusResponse): void {
    this.lastStatus = status;
    this.auditId = status.audit_id;
  }

  onAuditComplete(status: AccessibilityAuditStatusResponse): void {
    this.lastStatus = status;
    this.auditId = status.audit_id;
  }

  onViewFindings(auditId?: string): void {
    if (auditId) this.auditId = auditId;
    this.selectedTabIndex = 2;
    this.activeTab = 'findings';
  }

  onViewReport(auditId?: string): void {
    if (auditId) this.auditId = auditId;
    this.selectedTabIndex = 3;
    this.activeTab = 'report';
  }

  onRetestRequested(findingIds: string[]): void {
    if (!this.auditId) return;

    this.retestLoading = true;
    this.retestError = null;

    const request: RetestRequest = { finding_ids: findingIds };

    this.api.retestFindings(this.auditId, request).subscribe({
      next: (res) => {
        this.jobId = res.job_id;
        this.retestLoading = false;
        this.selectedTabIndex = 1;
        this.activeTab = 'status';
      },
      error: (err) => {
        this.retestError = err?.error?.detail ?? err?.message ?? 'Retest failed';
        this.retestLoading = false;
      },
    });
  }

  onExportRequested(format: 'json' | 'csv'): void {
    if (!this.auditId) return;

    this.api.downloadExport(this.auditId, format).subscribe({
      next: (blob) => {
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `accessibility-findings-${this.auditId}.${format}`;
        a.click();
        window.URL.revokeObjectURL(url);
      },
      error: (err) => {
        console.error('Export failed:', err);
      },
    });
  }

  startNewAudit(): void {
    this.jobId = null;
    this.auditId = null;
    this.lastJobResponse = null;
    this.lastStatus = null;
    this.selectedTabIndex = 0;
    this.activeTab = 'create';
  }

  get hasActiveJob(): boolean {
    return !!this.jobId;
  }

  get hasCompletedAudit(): boolean {
    return this.lastStatus?.status === 'complete';
  }
}
