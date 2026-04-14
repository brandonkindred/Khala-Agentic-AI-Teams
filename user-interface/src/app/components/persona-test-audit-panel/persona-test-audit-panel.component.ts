import { Component, inject, OnInit, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { Subscription, timer, EMPTY } from 'rxjs';
import { switchMap } from 'rxjs/operators';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTabsModule } from '@angular/material/tabs';
import { MatCardModule } from '@angular/material/card';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { PersonaTestingApiService } from '../../services/persona-testing-api.service';
import { PersonaChatComponent } from '../persona-chat/persona-chat.component';
import type { PersonaTestRunDetail, PersonaDecision, RunArtifacts } from '../../models';

const POLL_MS = 10_000;
const TERMINAL_STATUSES = ['completed', 'failed'];

@Component({
  selector: 'app-persona-test-audit-panel',
  standalone: true,
  imports: [
    CommonModule,
    RouterLink,
    MatButtonModule,
    MatIconModule,
    MatTabsModule,
    MatCardModule,
    MatExpansionModule,
    MatProgressBarModule,
    PersonaChatComponent,
  ],
  templateUrl: './persona-test-audit-panel.component.html',
  styleUrl: './persona-test-audit-panel.component.scss',
})
export class PersonaTestAuditPanelComponent implements OnInit, OnDestroy {
  private readonly api = inject(PersonaTestingApiService);
  private readonly route = inject(ActivatedRoute);
  private statusSub: Subscription | null = null;

  runId = '';
  run: PersonaTestRunDetail | null = null;
  artifacts: RunArtifacts | null = null;
  loading = true;
  error: string | null = null;

  ngOnInit(): void {
    this.runId = this.route.snapshot.paramMap.get('runId') ?? '';
    if (!this.runId) {
      this.error = 'No run ID provided';
      this.loading = false;
      return;
    }

    this.statusSub = timer(0, POLL_MS)
      .pipe(
        switchMap(() => {
          if (this.run && TERMINAL_STATUSES.includes(this.run.status)) {
            return EMPTY;
          }
          return this.api.getRunStatus(this.runId);
        }),
      )
      .subscribe({
        next: (detail) => {
          this.run = detail;
          this.loading = false;
          if (TERMINAL_STATUSES.includes(detail.status)) {
            this.loadArtifacts();
          }
        },
        error: (err) => {
          this.error = err?.error?.detail ?? 'Failed to load run status';
          this.loading = false;
        },
      });

    this.loadArtifacts();
  }

  ngOnDestroy(): void {
    this.statusSub?.unsubscribe();
  }

  private loadArtifacts(): void {
    this.api.getRunArtifacts(this.runId).subscribe({
      next: (a) => (this.artifacts = a),
    });
  }

  get isTerminal(): boolean {
    return !!this.run && TERMINAL_STATUSES.includes(this.run.status);
  }

  get decisions(): PersonaDecision[] {
    return this.run?.decisions ?? [];
  }

  get statusClass(): string {
    return this.run ? `status-${this.run.status}` : '';
  }

  formatStatus(status: string): string {
    return status.replace(/_/g, ' ');
  }

  get seJobProgress(): number | null {
    const s = this.artifacts?.se_job_status as Record<string, unknown> | undefined;
    if (!s) return null;
    return (s['progress'] as number) ?? null;
  }

  get seJobTaskStates(): Record<string, unknown> | null {
    const s = this.artifacts?.se_job_status as Record<string, unknown> | undefined;
    if (!s) return null;
    return (s['task_states'] as Record<string, unknown>) ?? null;
  }

  get seJobTaskIds(): string[] {
    const states = this.seJobTaskStates;
    return states ? Object.keys(states) : [];
  }

  getTaskStatus(taskId: string): string {
    const states = this.seJobTaskStates;
    if (!states) return '';
    const task = states[taskId] as Record<string, unknown> | undefined;
    return (task?.['status'] as string) ?? '';
  }

  getTaskTitle(taskId: string): string {
    const states = this.seJobTaskStates;
    if (!states) return taskId;
    const task = states[taskId] as Record<string, unknown> | undefined;
    return (task?.['title'] as string) ?? taskId;
  }
}
