import { Component, Input, Output, EventEmitter, OnInit, OnDestroy, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';
import { MatBadgeModule } from '@angular/material/badge';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import type { PlanningV2StatusResponse } from '../../models';

@Component({
  selector: 'app-planning-v2-job-status',
  standalone: true,
  imports: [
    CommonModule,
    MatCardModule,
    MatProgressBarModule,
    MatChipsModule,
    MatIconModule,
    MatBadgeModule,
  ],
  templateUrl: './planning-v2-job-status.component.html',
  styleUrl: './planning-v2-job-status.component.scss',
})
export class PlanningV2JobStatusComponent implements OnInit, OnDestroy {
  @Input() jobId!: string;
  /** Emits the latest status each time it's polled (including pending questions). */
  @Output() statusChange = new EventEmitter<PlanningV2StatusResponse>();

  private readonly api = inject(SoftwareEngineeringApiService);
  private pollTimer: ReturnType<typeof setInterval> | null = null;

  status: PlanningV2StatusResponse | null = null;
  error: string | null = null;

  readonly phases = [
    'planning',
    'implementation',
    'review',
    'problem_solving',
    'deliver',
  ];

  ngOnInit(): void {
    this.poll();
    this.pollTimer = setInterval(() => this.poll(), 120000);
  }

  ngOnDestroy(): void {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
    }
  }

  private poll(): void {
    this.api.getPlanningV2Status(this.jobId).subscribe({
      next: (res) => {
        this.status = res;
        this.error = null;
        this.statusChange.emit(res);
        if (res.status === 'completed' || res.status === 'failed' || res.status === 'cancelled') {
          if (this.pollTimer) {
            clearInterval(this.pollTimer);
            this.pollTimer = null;
          }
        }
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to fetch status';
      },
    });
  }

  /** Force an immediate poll (e.g., after answers are submitted). */
  refresh(): void {
    this.poll();
  }

  /** True when workflow is blocked waiting for user answers. */
  get isWaitingForAnswers(): boolean {
    return this.status?.waiting_for_answers ?? false;
  }

  /** Number of pending questions requiring answers. */
  get pendingQuestionsCount(): number {
    return this.status?.pending_questions?.length ?? 0;
  }

  phaseIcon(phase: string): string {
    if (!this.status) return 'radio_button_unchecked';
    if (this.status.completed_phases.includes(phase)) return 'check_circle';
    if (this.status.current_phase === phase) {
      if (this.isWaitingForAnswers) return 'pause_circle';
      return 'pending';
    }
    return 'radio_button_unchecked';
  }

  phaseClass(phase: string): string {
    if (!this.status) return '';
    if (this.status.completed_phases.includes(phase)) return 'phase-done';
    if (this.status.current_phase === phase) {
      if (this.isWaitingForAnswers) return 'phase-waiting';
      return 'phase-active';
    }
    return 'phase-pending';
  }

  phaseLabel(phase: string): string {
    const labels: Record<string, string> = {
      planning: 'Planning',
      implementation: 'Implementation',
      review: 'Review',
      problem_solving: 'Problem-solving',
      deliver: 'Deliver',
    };
    return labels[phase] ?? phase.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  }

  get activeRoles(): string[] {
    return this.status?.active_roles ?? [];
  }

  /** Format role for display (underscores to spaces). */
  formatRole(role: string): string {
    return role.replace(/_/g, ' ');
  }
}
