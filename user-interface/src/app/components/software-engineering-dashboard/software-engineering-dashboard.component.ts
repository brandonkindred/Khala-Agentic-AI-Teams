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
  launching = false;
  launchError: string | null = null;

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
   * Launch an SE project from the assistant context.
   * Creates a spec file from the context and uploads it via /run-team/upload.
   */
  launchProject(context: Record<string, unknown>): void {
    const spec = ((context['spec'] as string) ?? '').trim();
    if (!spec) {
      this.launchError = 'A project specification is required.';
      return;
    }
    this.launching = true;
    this.launchError = null;

    // Build a spec document from context fields
    let specContent = spec;
    const techStack = ((context['tech_stack'] as string) ?? '').trim();
    const constraints = ((context['constraints'] as string) ?? '').trim();
    if (techStack) specContent += `\n\n## Tech Stack\n${techStack}`;
    if (constraints) specContent += `\n\n## Constraints\n${constraints}`;

    const blob = new Blob([specContent], { type: 'text/markdown' });
    const file = new File([blob], 'initial_spec.md', { type: 'text/markdown' });
    const projectName = spec.substring(0, 60).replace(/[^a-zA-Z0-9 -]/g, '').trim() || 'New Project';

    this.api.runTeamFromUpload(projectName, file).subscribe({
      next: () => {
        this.launching = false;
        this.launchError = null;
        this.activeView = 'jobs';
      },
      error: (err: unknown) => {
        this.launching = false;
        const e = err as { error?: { detail?: string }; message?: string };
        this.launchError = e?.error?.detail ?? e?.message ?? 'Failed to start project';
      },
    });
  }
}
