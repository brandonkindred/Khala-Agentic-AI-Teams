import { Component, OnDestroy, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatChipsModule } from '@angular/material/chips';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatPaginatorModule, PageEvent } from '@angular/material/paginator';
import { RouterLink } from '@angular/router';
import { EMPTY, Subscription, interval } from 'rxjs';
import { catchError, switchMap, takeWhile } from 'rxjs/operators';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import type { GitHubIssueItem, RunGitHubIssueResponse } from '../../models/integrations.model';
import type { CodingTeamJobStatus } from '../../models/coding-team.model';
import { isCodingTeamTerminalStatus } from '../../models/job-status.model';

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
    MatTooltipModule,
    MatPaginatorModule,
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

  // Issue list pagination (client-side over the fully-fetched issue array)
  readonly PAGE_SIZE_OPTIONS = [10, 25, 50];
  pageSize = 10;
  pageIndex = 0;

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
        if (this.githubConfigured) {
          this.loadIssues();
        }
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
        this.pageIndex = 0;
        this.issuesLoaded = true;
        this.loadingIssues = false;
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.issueError = err?.error?.detail || err?.message || 'Failed to load issues.';
        this.loadingIssues = false;
      },
    });
  }

  /** The slice of issues visible on the current page. */
  get pagedIssues(): GitHubIssueItem[] {
    const start = this.pageIndex * this.pageSize;
    return this.issues.slice(start, start + this.pageSize);
  }

  onPageChange(event: PageEvent): void {
    this.pageIndex = event.pageIndex;
    this.pageSize = event.pageSize;
  }

  selectIssue(issue: GitHubIssueItem): void {
    this.selectedIssue = issue;
  }

  cancelSelection(): void {
    this.selectedIssue = null;
  }

  /** True when the issue is blocked by, or depends on, one or more other issues. */
  hasDependencies(issue: GitHubIssueItem): boolean {
    return issue.dependencies.length > 0;
  }

  /** The still-open dependencies rendered as "#3, #5" for warnings and tooltips. */
  openDepRefs(issue: GitHubIssueItem): string {
    return issue.dependencies
      .filter((d) => d.state === 'open')
      .map((d) => `#${d.number}`)
      .join(', ');
  }

  /** Hover/aria text describing the issue's dependencies and whether they block it. */
  dependencyTooltip(issue: GitHubIssueItem): string {
    if (issue.blocked) {
      return `Blocked by ${this.openDepRefs(issue)} — must be closed first`;
    }
    const refs = issue.dependencies.map((d) => `#${d.number}`).join(', ');
    return `Depends on ${refs} (all complete)`;
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

  private pollErrors = 0;

  private startPolling(jobId: string): void {
    this.stopPolling();
    this.pollErrors = 0;
    this.pollSub = interval(5000)
      .pipe(
        switchMap(() =>
          this.api.getJobStatus(jobId).pipe(
            catchError(() => {
              this.pollErrors++;
              if (this.pollErrors >= 3) {
                this.issueError = 'Lost connection to the coding team — status polling failed.';
                this.stopPolling();
              }
              return EMPTY;
            }),
          ),
        ),
        takeWhile((status) => !isCodingTeamTerminalStatus(status.status), true),
      )
      .subscribe({
        next: (status: CodingTeamJobStatus) => {
          this.pollErrors = 0;
          this.jobStatus = status;
        },
      });
  }

  private stopPolling(): void {
    if (this.pollSub) {
      this.pollSub.unsubscribe();
      this.pollSub = null;
    }
  }

  /** True once the active job has reached a terminal state (finished — pollable no further). */
  isJobTerminal(): boolean {
    return isCodingTeamTerminalStatus(this.jobStatus?.status);
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
