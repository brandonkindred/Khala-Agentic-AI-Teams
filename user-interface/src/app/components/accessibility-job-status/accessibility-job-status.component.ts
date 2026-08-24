import {
  Component,
  EventEmitter,
  Input,
  OnChanges,
  OnDestroy,
  Output,
  SimpleChanges,
  inject,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatIconModule } from '@angular/material/icon';
import { MatChipsModule } from '@angular/material/chips';
import { MatButtonModule } from '@angular/material/button';
import { Subscription, timer } from 'rxjs';
import { switchMap } from 'rxjs/operators';
import { AccessibilityApiService } from '../../services/accessibility-api.service';
import {
  ACCESSIBILITY_AUDIT_PHASES,
  type AccessibilityAuditStatusResponse,
  type AuditPhase,
} from '../../models';
import { extractErrorDetail } from '../../shared/extract-error-detail';

@Component({
  selector: 'app-accessibility-job-status',
  standalone: true,
  imports: [
    CommonModule,
    MatCardModule,
    MatProgressBarModule,
    MatIconModule,
    MatChipsModule,
    MatButtonModule,
  ],
  templateUrl: './accessibility-job-status.component.html',
  styleUrl: './accessibility-job-status.component.scss',
})
export class AccessibilityJobStatusComponent implements OnChanges, OnDestroy {
  private readonly api = inject(AccessibilityApiService);
  private pollSub: Subscription | null = null;

  @Input() jobId: string | null = null;
  @Output() statusChange = new EventEmitter<AccessibilityAuditStatusResponse>();
  @Output() auditComplete = new EventEmitter<AccessibilityAuditStatusResponse>();
  @Output() viewFindings = new EventEmitter<string>();
  @Output() viewReport = new EventEmitter<string>();

  status: AccessibilityAuditStatusResponse | null = null;
  error: string | null = null;

  readonly phases = ACCESSIBILITY_AUDIT_PHASES;

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['jobId'] && this.jobId) {
      this.startPolling();
    }
  }

  ngOnDestroy(): void {
    this.pollSub?.unsubscribe();
  }

  private startPolling(): void {
    this.pollSub?.unsubscribe();
    if (!this.jobId) return;

    this.pollSub = timer(0, 5000)
      .pipe(switchMap(() => this.api.getJobStatus(this.jobId!)))
      .subscribe({
        next: (res) => {
          this.status = res;
          this.error = null;
          this.statusChange.emit(res);

          if (res.status === 'complete' || res.status === 'failed' || res.status === 'cancelled') {
            this.pollSub?.unsubscribe();
            if (res.status === 'complete') {
              this.auditComplete.emit(res);
            }
          }
        },
        error: (err) => {
          this.error = extractErrorDetail(err, 'Failed to fetch status');
        },
      });
  }

  getPhaseStatus(phaseId: AuditPhase): 'completed' | 'current' | 'pending' {
    if (!this.status?.current_phase) return 'pending';

    const completedPhases = this.status.completed_phases || [];
    if (completedPhases.includes(phaseId)) return 'completed';

    if (this.status.current_phase === phaseId) return 'current';

    return 'pending';
  }

  getPhaseIcon(phaseId: AuditPhase): string {
    const phase = this.phases.find((p) => p.id === phaseId);
    return phase?.icon || 'radio_button_unchecked';
  }

  getStatusIcon(jobStatus: string): string {
    switch (jobStatus) {
      case 'complete':
        return 'check_circle';
      case 'failed':
        return 'error';
      case 'cancelled':
        return 'cancel';
      case 'running':
        return 'play_circle';
      default:
        return 'hourglass_empty';
    }
  }

  getStatusColor(jobStatus: string): string {
    switch (jobStatus) {
      case 'complete':
        return 'primary';
      case 'failed':
        return 'warn';
      case 'cancelled':
        return 'accent';
      default:
        return 'accent';
    }
  }

  getSeverityClass(severity: string): string {
    return severity.toLowerCase();
  }

  onViewFindings(): void {
    if (this.status?.audit_id) {
      this.viewFindings.emit(this.status.audit_id);
    }
  }

  onViewReport(): void {
    if (this.status?.audit_id) {
      this.viewReport.emit(this.status.audit_id);
    }
  }
}
