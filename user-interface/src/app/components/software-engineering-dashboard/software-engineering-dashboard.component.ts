import { Component, inject, OnInit, OnDestroy } from '@angular/core';
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
import type { RunningJobSummary } from '../../models';

const POLL_JOBS_MS = 30_000;
const TERMINAL_STATUSES = ['completed', 'failed', 'cancelled', 'stopped'];

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
  private jobsSub: Subscription | null = null;

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
        if (this.activeView === 'empty') {
          this.activeView = this.allJobs.length > 0 ? 'jobs' : 'empty';
        }
      },
    });
  }

  ngOnDestroy(): void {
    this.jobsSub?.unsubscribe();
  }

  showNewProject(): void {
    this.activeView = 'new-project';
  }

  showJobs(): void {
    this.activeView = 'jobs';
  }

  /**
   * Handle a launch that went through the backend `/assistant/launch` endpoint.
   * The backend's SE body builder produces the same multipart spec upload this
   * dashboard used to build client-side, so we only need to navigate to jobs;
   * the polling loop in ngOnInit will pick the new job up automatically.
   */
  onWorkflowLaunched(event: { job_id: string | null; conversation_id: string }): void {
    void event;
    this.activeView = 'jobs';
  }
}
