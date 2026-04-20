import { Component, inject, OnDestroy, OnInit } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { CommonModule } from '@angular/common';
import { FormBuilder, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatTabsModule } from '@angular/material/tabs';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatChipsModule } from '@angular/material/chips';
import { MatTableModule } from '@angular/material/table';
import { MatTooltipModule } from '@angular/material/tooltip';
import { Observable, Subscription, interval, switchMap, takeWhile } from 'rxjs';
import { AgentProvisioningApiService } from '../../services/agent-provisioning-api.service';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import { DashboardShellComponent } from '../../shared/dashboard-shell/dashboard-shell.component';
import type {
  ProvisionRequest,
  ProvisionStatusResponse,
  ProvisionJobSummary,
  AgentStatusResponse,
  AccessTier,
} from '../../models';
import { PROVISIONING_PHASES } from '../../models';

type DashboardTab = 'provision' | 'jobs' | 'environments';

@Component({
  selector: 'app-agent-provisioning-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    MatTabsModule,
    MatIconModule,
    MatButtonModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatProgressBarModule,
    MatChipsModule,
    MatTableModule,
    MatTooltipModule,
    TeamAssistantChatComponent,
    DashboardShellComponent,
  ],
  templateUrl: './agent-provisioning-dashboard.component.html',
  styleUrl: './agent-provisioning-dashboard.component.scss',
})
export class AgentProvisioningDashboardComponent implements OnInit, OnDestroy {
  private readonly api = inject(AgentProvisioningApiService);
  private readonly fb = inject(FormBuilder);
  private readonly route = inject(ActivatedRoute);

  private jobPollSub: Subscription | null = null;
  private queryParamsSub: Subscription | null = null;
  private pendingJobId: string | null = null;

  selectedTabIndex = 0;
  activeTab: DashboardTab = 'provision';

  healthCheck: () => Observable<{ status?: string }> = () => this.api.healthCheck();

  provisionForm: FormGroup;
  submitting = false;
  submitError: string | null = null;

  currentJobId: string | null = null;
  currentJobStatus: ProvisionStatusResponse | null = null;

  jobs: ProvisionJobSummary[] = [];
  jobsLoading = false;

  agents: AgentStatusResponse[] = [];
  agentsLoading = false;

  readonly phases = PROVISIONING_PHASES;
  readonly accessTiers: AccessTier[] = ['minimal', 'standard', 'elevated', 'full'];

  readonly jobColumns = ['job_id', 'agent_id', 'status', 'current_phase', 'progress'];
  readonly agentColumns = ['agent_id', 'status', 'tools', 'actions'];

  constructor() {
    this.provisionForm = this.fb.group({
      agent_id: ['', Validators.required],
      manifest_path: ['default.yaml'],
      access_tier: ['standard' as AccessTier],
      workspace_path: [''],
    });
  }

  ngOnInit(): void {
    this.queryParamsSub = this.route.queryParams.subscribe((params) => {
      const id = params['jobId'];
      if (id) this.pendingJobId = id;
    });
    this.loadJobs();
    this.loadAgents();
  }

  ngOnDestroy(): void {
    this.queryParamsSub?.unsubscribe();
    this.jobPollSub?.unsubscribe();
  }

  onTabChange(index: number): void {
    this.selectedTabIndex = index;
    const tabs: DashboardTab[] = ['provision', 'jobs', 'environments'];
    this.activeTab = tabs[index] || 'provision';

    if (this.activeTab === 'jobs') {
      this.loadJobs();
    } else if (this.activeTab === 'environments') {
      this.loadAgents();
    }
  }

  onSubmitProvision(): void {
    if (this.provisionForm.invalid) return;

    this.submitting = true;
    this.submitError = null;

    const request: ProvisionRequest = {
      agent_id: this.provisionForm.value.agent_id,
      manifest_path: this.provisionForm.value.manifest_path || 'default.yaml',
      access_tier: this.provisionForm.value.access_tier || 'standard',
      workspace_path: this.provisionForm.value.workspace_path || undefined,
    };

    this.api.startProvisioning(request).subscribe({
      next: (res) => {
        this.currentJobId = res.job_id;
        this.submitting = false;
        this.startJobPolling(res.job_id);
      },
      error: (err) => {
        this.submitError = err?.error?.detail ?? err?.message ?? 'Failed to start provisioning';
        this.submitting = false;
      },
    });
  }

  /** Handle a launch triggered from the assistant chat. */
  onAssistantLaunched(event: { job_id: string | null; conversation_id: string }): void {
    if (event.job_id) {
      this.currentJobId = event.job_id;
      this.startJobPolling(event.job_id);
    }
  }

  private startJobPolling(jobId: string): void {
    this.jobPollSub?.unsubscribe();

    this.jobPollSub = interval(20000)
      .pipe(
        switchMap(() => this.api.getJobStatus(jobId)),
        takeWhile((status) => status.status === 'running' || status.status === 'pending', true)
      )
      .subscribe({
        next: (status) => {
          this.currentJobStatus = status;
        },
        error: (err) => {
          console.error('Job polling error:', err);
        },
      });
  }

  loadJobs(): void {
    this.jobsLoading = true;
    this.api.listJobs().subscribe({
      next: (res) => {
        this.jobs = res.jobs;
        this.jobsLoading = false;
        if (this.pendingJobId != null) {
          this.viewJobStatus(this.pendingJobId);
          this.pendingJobId = null;
        }
      },
      error: (err) => {
        console.error('Failed to load jobs:', err);
        this.jobsLoading = false;
      },
    });
  }

  loadAgents(): void {
    this.agentsLoading = true;
    this.api.listAgents().subscribe({
      next: (res) => {
        this.agents = res.agents;
        this.agentsLoading = false;
      },
      error: (err) => {
        console.error('Failed to load agents:', err);
        this.agentsLoading = false;
      },
    });
  }

  deprovisionAgent(agentId: string): void {
    if (!confirm(`Are you sure you want to deprovision agent "${agentId}"?`)) return;

    this.api.deprovisionAgent(agentId).subscribe({
      next: () => {
        this.loadAgents();
      },
      error: (err) => {
        console.error('Failed to deprovision agent:', err);
      },
    });
  }

  viewJobStatus(jobId: string): void {
    this.currentJobId = jobId;
    this.api.getJobStatus(jobId).subscribe({
      next: (status) => {
        this.currentJobStatus = status;
        this.selectedTabIndex = 0;
        this.activeTab = 'provision';
        if (status.status === 'running' || status.status === 'pending') {
          this.startJobPolling(jobId);
        }
      },
      error: (err) => {
        console.error('Failed to load job status:', err);
      },
    });
  }

  clearCurrentJob(): void {
    this.currentJobId = null;
    this.currentJobStatus = null;
    this.jobPollSub?.unsubscribe();
    this.provisionForm.reset({
      agent_id: '',
      manifest_path: 'default.yaml',
      access_tier: 'standard',
      workspace_path: '',
    });
  }

  get canStopCurrentJob(): boolean {
    const status = this.currentJobStatus?.status;
    return status === 'pending' || status === 'running';
  }

  stopCurrentJob(): void {
    if (!this.currentJobId) return;
    this.api.cancelJob(this.currentJobId).subscribe({
      next: () => {
        this.jobPollSub?.unsubscribe();
        this.jobPollSub = null;
        this.api.getJobStatus(this.currentJobId!).subscribe({
          next: (status) => {
            this.currentJobStatus = status;
          },
        });
      },
      error: (err) => {
        console.error('Failed to cancel job:', err);
        this.submitError = err?.error?.detail ?? err?.message ?? 'Failed to cancel job';
      },
    });
  }

  deleteCurrentJob(): void {
    if (!this.currentJobId) return;
    if (!confirm('Delete this job? This cannot be undone.')) return;
    const id = this.currentJobId;
    this.api.deleteJob(id).subscribe({
      next: () => {
        this.clearCurrentJob();
        this.loadJobs();
      },
      error: (err) => {
        console.error('Failed to delete job:', err);
        this.submitError = err?.error?.detail ?? err?.message ?? 'Failed to delete job';
      },
    });
  }

  getPhaseLabel(phaseId: string): string {
    return this.phases.find(p => p.id === phaseId)?.label ?? phaseId;
  }

  isPhaseCompleted(phaseId: string): boolean {
    return this.currentJobStatus?.completed_phases?.includes(phaseId) ?? false;
  }

  isPhaseCurrent(phaseId: string): boolean {
    return this.currentJobStatus?.current_phase === phaseId;
  }

  get isJobActive(): boolean {
    return this.currentJobStatus?.status === 'running' || this.currentJobStatus?.status === 'pending';
  }

  get isJobComplete(): boolean {
    return this.currentJobStatus?.status === 'completed';
  }

  get isJobFailed(): boolean {
    return this.currentJobStatus?.status === 'failed';
  }
}
