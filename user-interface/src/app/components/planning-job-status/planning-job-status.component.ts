import { Component, Input, Output, EventEmitter, OnInit, OnDestroy, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatIconModule } from '@angular/material/icon';
import { Subject, takeUntil } from 'rxjs';
import { PlanningApiService } from '../../services/planning-api.service';
import { PLANNING_PHASES } from '../../models';
import type { PlanningStatusResponse, PlanningResultResponse } from '../../models';
import { InlineBannerComponent } from '../../shared/inline-banner/inline-banner.component';
import { extractErrorDetail } from '../../shared/extract-error-detail';

const PHASES = PLANNING_PHASES.map((p) => p.id);

/** Displays the current status and progress of a Planning job, polling the API until completion. */
@Component({
  selector: 'app-planning-job-status',
  standalone: true,
  imports: [
    CommonModule,
    MatCardModule,
    MatProgressBarModule,
    MatIconModule,
    InlineBannerComponent,
  ],
  templateUrl: './planning-job-status.component.html',
  styleUrl: './planning-job-status.component.scss',
})
export class PlanningJobStatusComponent implements OnInit, OnDestroy {
  @Input() jobId!: string;
  @Output() statusChange = new EventEmitter<PlanningStatusResponse>();

  private readonly api = inject(PlanningApiService);
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private readonly destroy$ = new Subject<void>();

  status: PlanningStatusResponse | null = null;
  result: PlanningResultResponse | null = null;
  error: string | null = null;

  readonly phases = PHASES;

  /** Fetch status immediately, then poll every 15s until the job reaches a terminal state. */
  ngOnInit(): void {
    this.poll();
    this.pollTimer = setInterval(() => this.poll(), 15000);
  }

  /** Stop the poll timer and any in-flight requests so they can't update a destroyed component. */
  ngOnDestroy(): void {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
    this.destroy$.next();
    this.destroy$.complete();
  }

  /** Fetch the job's status; on `completed`/`failed` stop the timer, and on `completed` also fetch the result. */
  private poll(): void {
    this.api
      .getStatus(this.jobId)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (res) => {
          this.status = res;
          this.error = null;
          this.statusChange.emit(res);
          if (res.status === 'completed' || res.status === 'failed') {
            if (this.pollTimer) {
              clearInterval(this.pollTimer);
              this.pollTimer = null;
            }
            if (res.status === 'completed') {
              this.api
                .getResult(this.jobId)
                .pipe(takeUntil(this.destroy$))
                .subscribe({
                  next: (r) => (this.result = r),
                  error: (err) => {
                    this.error = extractErrorDetail(err, 'Failed to load planning result');
                  },
                });
            }
          }
        },
        error: (err) => {
          this.error = extractErrorDetail(err, 'Failed to fetch status');
        },
      });
  }

  /** Manually re-fetch status on demand (e.g. a user-triggered refresh button). */
  refresh(): void {
    this.poll();
  }

  get isWaitingForAnswers(): boolean {
    return this.status?.waiting_for_answers ?? false;
  }

  get pendingQuestionsCount(): number {
    return this.status?.pending_questions?.length ?? 0;
  }

  phaseIcon(phase: string): string {
    if (!this.status) return 'radio_button_unchecked';
    const current = this.status.current_phase;
    if (current === phase) {
      if (this.isWaitingForAnswers) return 'pause_circle';
      return 'pending';
    }
    const idx = this.phases.indexOf(phase);
    const curIdx = current ? this.phases.indexOf(current) : -1;
    return idx < curIdx ? 'check_circle' : 'radio_button_unchecked';
  }

  phaseClass(phase: string): string {
    if (!this.status) return '';
    if (this.status.current_phase === phase) {
      if (this.isWaitingForAnswers) return 'phase-waiting';
      return 'phase-active';
    }
    const idx = this.phases.indexOf(phase);
    const curIdx = this.status.current_phase ? this.phases.indexOf(this.status.current_phase) : -1;
    return idx < curIdx ? 'phase-done' : 'phase-pending';
  }

  phaseLabel(phase: string): string {
    const labels: Record<string, string> = {
      intake: 'Intake',
      discovery: 'Discovery',
      requirements: 'Requirements',
      synthesis: 'Synthesis',
      document_production: 'Document production',
      sub_agent_provisioning: 'Sub-agent (optional)',
    };
    return labels[phase] ?? phase.replace(/_/g, ' ');
  }
}
