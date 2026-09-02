import { Component, ElementRef, inject, OnInit, OnDestroy } from '@angular/core';
import { RouterLink } from '@angular/router';
import { CommonModule } from '@angular/common';
import { Subscription, timer } from 'rxjs';
import { switchMap } from 'rxjs/operators';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import { DashboardShellComponent } from '../../shared/dashboard-shell/dashboard-shell.component';
import { deferFocus } from '../../shared/defer-focus';
import type { RunningJobSummary } from '../../models';
import { CODING_TEAM_TERMINAL_STATUSES } from '../../models/job-status.model';

const POLL_JOBS_MS = 30_000;
// SE jobs share the coding-team terminal set, plus 'stopped' (an SE-only terminal state).
const TERMINAL_STATUSES = [...CODING_TEAM_TERMINAL_STATUSES, 'stopped'];

@Component({
  selector: 'app-software-engineering-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    RouterLink,
    MatButtonModule,
    MatIconModule,
    MatProgressBarModule,
    TeamAssistantChatComponent,
    DashboardShellComponent,
  ],
  templateUrl: './software-engineering-dashboard.component.html',
  styleUrl: './software-engineering-dashboard.component.scss',
})
export class SoftwareEngineeringDashboardComponent implements OnInit, OnDestroy {
  private readonly api = inject(SoftwareEngineeringApiService);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private jobsSub: Subscription | null = null;
  private focusTimer: ReturnType<typeof setTimeout> | null = null;

  activeView: 'empty' | 'new-project' | 'jobs' = 'empty';

  allJobs: RunningJobSummary[] = [];
  runningJobs: RunningJobSummary[] = [];
  completedJobs: RunningJobSummary[] = [];

  isTerminal(status: string): boolean {
    return TERMINAL_STATUSES.includes(status);
  }

  ngOnInit(): void {
    this.jobsSub = timer(0, POLL_JOBS_MS).pipe(
      switchMap(() => this.api.getRunningJobs(false))
    ).subscribe({
      next: (resp) => {
        this.allJobs = resp.jobs ?? [];
        this.runningJobs = this.allJobs.filter((j) => !this.isTerminal(j.status));
        this.completedJobs = this.allJobs.filter((j) => this.isTerminal(j.status));
        // Poll-driven view change only — never a user action, so this must
        // never move focus (it would steal focus from someone reading the
        // empty state). Do not route this through moveFocusTo().
        if (this.activeView === 'empty') {
          this.activeView = this.allJobs.length > 0 ? 'jobs' : 'empty';
        }
      },
    });
  }

  ngOnDestroy(): void {
    this.jobsSub?.unsubscribe();
    if (this.focusTimer !== null) {
      clearTimeout(this.focusTimer);
    }
  }

  /**
   * Show the new-project form.
   *
   * Preconditions: none.
   * Postconditions: `activeView` is `'new-project'`; after the next render
   *   tick, keyboard focus is inside `.new-project-view`.
   */
  showNewProject(): void {
    this.activeView = 'new-project';
    this.moveFocusTo('.new-project-view');
  }

  /**
   * Show the jobs list.
   *
   * Preconditions: none.
   * Postconditions: `activeView` is `'jobs'`; after the next render tick,
   *   keyboard focus is inside `.jobs-list-view`.
   */
  showJobs(): void {
    this.activeView = 'jobs';
    this.moveFocusTo('.jobs-list-view');
  }

  /**
   * Handle a launch that went through the backend `/assistant/launch` endpoint.
   * The backend's SE body builder produces the same multipart spec upload this
   * dashboard used to build client-side, so we only need to navigate to jobs;
   * the polling loop in ngOnInit will pick the new job up automatically.
   *
   * Preconditions: none.
   * Postconditions: `activeView` is `'jobs'`; after the next render tick,
   *   keyboard focus is inside `.jobs-list-view`. This *recovers* focus
   *   rather than merely preserving it — the Launch button's
   *   `[disabled]="loading"` binding already blurred it to `<body>` before
   *   this handler runs.
   */
  onWorkflowLaunched(event: { job_id: string | null; conversation_id: string }): void {
    void event;
    this.activeView = 'jobs';
    this.moveFocusTo('.jobs-list-view');
  }

  /**
   * Move keyboard focus into the element matching `selector` within this
   * component's host, once Angular has rendered the pending view change.
   *
   * Preconditions: `selector` matches a rendered element carrying
   *   `tabindex="-1"` once the view swap commits.
   * Postconditions: any previously pending focus move is cancelled and
   *   superseded by this one; the new timer handle is stored in
   *   `focusTimer` so `ngOnDestroy` can clear it.
   */
  private moveFocusTo(selector: string): void {
    if (this.focusTimer !== null) {
      clearTimeout(this.focusTimer);
    }
    this.focusTimer = deferFocus(this.host.nativeElement, (root) =>
      root.querySelector<HTMLElement>(selector)
    );
  }
}
