import { Component, OnDestroy, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatPaginatorModule, PageEvent } from '@angular/material/paginator';
import { RouterLink } from '@angular/router';
import { EMPTY, Subscription, interval } from 'rxjs';
import { catchError, switchMap, takeWhile } from 'rxjs/operators';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import type { GitHubPullRequestItem, RunPrReviewResponse } from '../../models/integrations.model';
import type { CodingTeamJobStatus } from '../../models/coding-team.model';
import { isCodingTeamTerminalStatus } from '../../models/job-status.model';

/**
 * Code Review panel: lists open pull requests from the configured GitHub repo and
 * lets the user start an AI code review on one. The review runs the Software
 * Engineering code-reviewer agents, which post a GitHub review with inline comments.
 */
@Component({
  selector: 'app-code-review-panel',
  standalone: true,
  imports: [
    CommonModule,
    MatIconModule,
    MatButtonModule,
    MatProgressSpinnerModule,
    MatTooltipModule,
    MatPaginatorModule,
    RouterLink,
    HealthIndicatorComponent,
  ],
  templateUrl: './code-review-panel.component.html',
  styleUrl: './code-review-panel.component.scss',
})
export class CodeReviewPanelComponent implements OnInit, OnDestroy {
  private readonly api = inject(CodingTeamApiService);
  private readonly integrationsApi = inject(IntegrationsApiService);

  healthCheck = (): ReturnType<CodingTeamApiService['health']> => this.api.health();

  // GitHub integration state
  githubConfigured = false;
  githubOwner = '';
  githubRepo = '';
  loadingConfig = true;

  // Pull-request list
  pulls: GitHubPullRequestItem[] = [];
  loadingPulls = false;
  pullsLoaded = false;
  pullError: string | null = null;

  // Client-side pagination over the fully-fetched PR array
  readonly PAGE_SIZE_OPTIONS = [10, 25, 50];
  pageSize = 10;
  pageIndex = 0;

  // Selection & confirmation
  selectedPull: GitHubPullRequestItem | null = null;
  startingReview = false;

  // Active review job tracking
  activeJob: RunPrReviewResponse | null = null;
  jobStatus: CodingTeamJobStatus | null = null;
  private pollSub: Subscription | null = null;
  private pollErrors = 0;

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
          this.loadPulls();
        }
      },
      error: () => {
        this.githubConfigured = false;
        this.loadingConfig = false;
      },
    });
  }

  loadPulls(): void {
    this.loadingPulls = true;
    this.pullError = null;
    this.selectedPull = null;
    this.integrationsApi.getGitHubPullRequests().subscribe({
      next: (pulls) => {
        this.pulls = pulls;
        this.pageIndex = 0;
        this.pullsLoaded = true;
        this.loadingPulls = false;
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.pullError = err?.error?.detail || err?.message || 'Failed to load pull requests.';
        this.loadingPulls = false;
      },
    });
  }

  /** The slice of PRs visible on the current page. */
  get pagedPulls(): GitHubPullRequestItem[] {
    const start = this.pageIndex * this.pageSize;
    return this.pulls.slice(start, start + this.pageSize);
  }

  onPageChange(event: PageEvent): void {
    this.pageIndex = event.pageIndex;
    this.pageSize = event.pageSize;
  }

  selectPull(pull: GitHubPullRequestItem): void {
    this.selectedPull = pull;
  }

  cancelSelection(): void {
    this.selectedPull = null;
  }

  confirmAndReview(): void {
    if (!this.selectedPull) return;
    this.startingReview = true;
    this.pullError = null;
    this.integrationsApi.runGitHubReviewPr({ pr_number: this.selectedPull.number }).subscribe({
      next: (resp: RunPrReviewResponse) => {
        this.activeJob = resp;
        this.selectedPull = null;
        this.startingReview = false;
        this.startPolling(resp.job_id);
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.pullError = err?.error?.detail || err?.message || 'Failed to start review.';
        this.startingReview = false;
      },
    });
  }

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
                this.pullError = 'Lost connection to the coding team — status polling failed.';
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

  /** True once the active review job has reached a terminal state. */
  isJobTerminal(): boolean {
    return isCodingTeamTerminalStatus(this.jobStatus?.status);
  }

  dismissJob(): void {
    this.stopPolling();
    this.activeJob = null;
    this.jobStatus = null;
  }
}
