import { Component, Input, OnInit, OnDestroy, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';
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
  ],
  templateUrl: './planning-v2-job-status.component.html',
  styleUrl: './planning-v2-job-status.component.scss',
})
export class PlanningV2JobStatusComponent implements OnInit, OnDestroy {
  @Input() jobId!: string;

  private readonly api = inject(SoftwareEngineeringApiService);
  private pollTimer: ReturnType<typeof setInterval> | null = null;

  status: PlanningV2StatusResponse | null = null;
  error: string | null = null;

  readonly phases = [
    'spec_review_gap',
    'planning',
    'implementation',
    'review',
    'problem_solving',
    'deliver',
  ];

  ngOnInit(): void {
    this.poll();
    this.pollTimer = setInterval(() => this.poll(), 15000);
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
        if (res.status === 'completed' || res.status === 'failed') {
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

  phaseIcon(phase: string): string {
    if (!this.status) return 'radio_button_unchecked';
    if (this.status.completed_phases.includes(phase)) return 'check_circle';
    if (this.status.current_phase === phase) return 'pending';
    return 'radio_button_unchecked';
  }

  phaseClass(phase: string): string {
    if (!this.status) return '';
    if (this.status.completed_phases.includes(phase)) return 'phase-done';
    if (this.status.current_phase === phase) return 'phase-active';
    return 'phase-pending';
  }

  phaseLabel(phase: string): string {
    const labels: Record<string, string> = {
      spec_review_gap: 'Spec Review & Gap',
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
}
