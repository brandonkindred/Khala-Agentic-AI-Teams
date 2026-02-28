import { Component, Input, OnInit, OnDestroy, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import type { FrontendCodeV2StatusResponse } from '../../models';

@Component({
  selector: 'app-frontend-code-v2-job-status',
  standalone: true,
  imports: [
    CommonModule,
    MatCardModule,
    MatProgressBarModule,
    MatChipsModule,
    MatIconModule,
  ],
  templateUrl: './frontend-code-v2-job-status.component.html',
  styleUrl: './frontend-code-v2-job-status.component.scss',
})
export class FrontendCodeV2JobStatusComponent implements OnInit, OnDestroy {
  @Input() jobId!: string;

  private readonly api = inject(SoftwareEngineeringApiService);
  private pollTimer: ReturnType<typeof setInterval> | null = null;

  status: FrontendCodeV2StatusResponse | null = null;
  error: string | null = null;

  readonly phases = ['setup', 'planning', 'execution', 'review', 'problem_solving', 'deliver'];

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
    this.api.getFrontendCodeV2Status(this.jobId).subscribe({
      next: (res) => {
        this.status = res;
        this.error = null;
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
    return phase.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  }
}
