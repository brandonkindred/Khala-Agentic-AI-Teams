import { Component, OnDestroy, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatChipsModule } from '@angular/material/chips';
import { RouterLink } from '@angular/router';
import { Subscription, interval } from 'rxjs';
import { switchMap, takeWhile } from 'rxjs/operators';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import type { GitHubIssueItem, RunGitHubIssueResponse } from '../../models/integrations.model';
import type { CodingTeamJobStatus } from '../../models/coding-team.model';

@Component({
  selector: 'app-coding-team-page',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatIconModule,
    MatButtonModule,
    MatProgressSpinnerModule,
    MatChipsModule,
    RouterLink,
    HealthIndicatorComponent,
    TeamAssistantChatComponent,
  ],
  templateUrl: './coding-team-page.component.html',
  styleUrl: './coding-team-page.component.scss',
})
export class CodingTeamPageComponent implements OnInit, OnDestroy {
  private readonly api = inject(CodingTeamApiService);
  private readonly integrationsApi = inject(IntegrationsApiService);

  latestJobId: string | null = null;

  healthCheck = (): ReturnType<CodingTeamApiService['health']> => this.api.health();

  // GitHub integration state
  githubConfigured = false;
  githubOwner = '';
  githubRepo = '';
  loadingConfig = true;

  // Issue list
  issues: GitHubIssueItem[] = [];
  loadingIssues = false;
  issuesLoaded = false;
  issueError: string | null = null;

  // Issue selection & confirmation
  selectedIssue: GitHubIssueItem | null = null;
  runningIssue = false;

  // Active job tracking
  activeJob: RunGitHubIssueResponse | null = null;
  jobStatus: CodingTeamJobStatus | null = null;
  private pollSub: Subscription | null = null;

  ngOnInit(): void {
    this.checkGitHubConfig();
  }

  ngOnDestroy(): void {
    this.stopPolling();
  }

  checkGitHubConfig(): void {
    this.loadingConfig = true;
    this.integrationsApi.getGitHubConfig().subscribe({
      next: (cfg) => {
        this.githubConfigured = cfg.enabled && cfg.token_configured && !!cfg.owner && !!cfg.repo;
        this.githubOwner = cfg.owner;
        this.githubRepo = cfg.repo;
        this.loadingConfig = false;
      },
      error: () => {
        this.githubConfigured = false;
        this.loadingConfig = false;
      },
    });
  }

  loadIssues(): void {
    this.loadingIssues = true;
    this.issueError = null;
    this.selectedIssue = null;
    this.integrationsApi.getGitHubIssues().subscribe({
      next: (issues) => {
        this.issues = issues;
        this.issuesLoaded = true;
        this.loadingIssues = false;
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.issueError = err?.error?.detail || err?.message || 'Failed to load issues.';
        this.loadingIssues = false;
      },
    });
  }

  selectIssue(issue: GitHubIssueItem): void {
    this.selectedIssue = issue;
  }

  cancelSelection(): void {
    this.selectedIssue = null;
  }

  confirmAndRun(): void {
    if (!this.selectedIssue) return;
    this.runningIssue = true;
    this.issueError = null;
    this.integrationsApi.runGitHubIssue({ issue_number: this.selectedIssue.number }).subscribe({
      next: (resp: RunGitHubIssueResponse) => {
        this.activeJob = resp;
        this.selectedIssue = null;
        this.runningIssue = false;
        this.startPolling(resp.job_id);
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.issueError = err?.error?.detail || err?.message || 'Failed to start job.';
        this.runningIssue = false;
      },
    });
  }

  private startPolling(jobId: string): void {
    this.stopPolling();
    this.pollSub = interval(5000)
      .pipe(
        switchMap(() => this.api.getJobStatus(jobId)),
        takeWhile((status) => status.status !== 'completed' && status.status !== 'failed', true),
      )
      .subscribe({
        next: (status: CodingTeamJobStatus) => {
          this.jobStatus = status;
        },
        error: () => {
          // polling error — job might have been cleaned up
        },
      });
  }

  private stopPolling(): void {
    if (this.pollSub) {
      this.pollSub.unsubscribe();
      this.pollSub = null;
    }
  }

  dismissJob(): void {
    this.stopPolling();
    this.activeJob = null;
    this.jobStatus = null;
  }

  onWorkflowLaunched(event: { job_id: string | null; conversation_id: string }): void {
    this.latestJobId = event.job_id;
  }
}
