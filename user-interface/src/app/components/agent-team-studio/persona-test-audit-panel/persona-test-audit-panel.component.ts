import { Component, DestroyRef, inject, Input, OnInit } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTabsModule } from '@angular/material/tabs';
import { MatCardModule } from '@angular/material/card';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { PersonaTestingApiService } from '../../../services/persona-testing-api.service';
import { PersonaChatComponent } from '../persona-chat/persona-chat.component';
import { pollWhile } from '../../../shared/poll-while';
import { isPersonaRunTerminal } from '../../../models';
import type { PersonaTestRunDetail, PersonaDecision, RunArtifacts } from '../../../models';

const POLL_MS = 10_000;

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
export class PersonaTestAuditPanelComponent implements OnInit {
  private readonly api = inject(PersonaTestingApiService);
  private readonly route = inject(ActivatedRoute);
  private readonly destroyRef = inject(DestroyRef);

  /**
   * Router path for the header back control.
   *
   * Preconditions: a non-empty absolute-from-root path (leading `/`).
   * Postconditions: the template's back `routerLink` equals this value.
   */
  @Input() backLink = '/agent-studio';

  /**
   * Visible label for the header back control.
   *
   * Preconditions: a non-empty string.
   * Postconditions: the template renders this text next to the back icon.
   */
  @Input() backLabel = 'Back to Agent Studio';

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

    pollWhile(
      () => this.api.getRunStatus(this.runId),
      (detail) => isPersonaRunTerminal(detail.status),
      { intervalMs: POLL_MS, onError: 'stop' },
    )
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (detail) => {
          this.run = detail;
          this.loading = false;
          if (isPersonaRunTerminal(detail.status)) {
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

  private loadArtifacts(): void {
    this.api.getRunArtifacts(this.runId).subscribe({
      next: (a) => (this.artifacts = a),
    });
  }

  get isTerminal(): boolean {
    return !!this.run && isPersonaRunTerminal(this.run.status);
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
