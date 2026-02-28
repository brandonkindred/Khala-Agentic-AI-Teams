import { Component, EventEmitter, Input, OnChanges, OnDestroy, Output, SimpleChanges, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatIconModule } from '@angular/material/icon';
import { MatChipsModule } from '@angular/material/chips';
import { Subscription, timer } from 'rxjs';
import { switchMap } from 'rxjs/operators';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import type { ProductAnalysisStatusResponse } from '../../models';

@Component({
  selector: 'app-product-analysis-job-status',
  standalone: true,
  imports: [
    CommonModule,
    MatCardModule,
    MatProgressBarModule,
    MatIconModule,
    MatChipsModule,
  ],
  templateUrl: './product-analysis-job-status.component.html',
  styleUrl: './product-analysis-job-status.component.scss',
})
export class ProductAnalysisJobStatusComponent implements OnChanges, OnDestroy {
  private readonly api = inject(SoftwareEngineeringApiService);
  private pollSub: Subscription | null = null;

  @Input() jobId: string | null = null;
  @Output() statusChange = new EventEmitter<ProductAnalysisStatusResponse>();

  status: ProductAnalysisStatusResponse | null = null;
  error: string | null = null;

  readonly phases = [
    { id: 'spec_review', label: 'Spec Review' },
    { id: 'communicate', label: 'Communicate' },
    { id: 'spec_update', label: 'Spec Update' },
    { id: 'spec_cleanup', label: 'Spec Cleanup' },
  ];

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
      .pipe(switchMap(() => this.api.getProductAnalysisStatus(this.jobId!)))
      .subscribe({
        next: (res) => {
          this.status = res;
          this.error = null;
          this.statusChange.emit(res);

          if (res.status === 'completed' || res.status === 'failed' || res.status === 'cancelled') {
            this.pollSub?.unsubscribe();
          }
        },
        error: (err) => {
          this.error = err?.error?.detail ?? err?.message ?? 'Failed to fetch status';
        },
      });
  }

  getPhaseStatus(phaseId: string): 'completed' | 'current' | 'pending' {
    if (!this.status?.current_phase) return 'pending';

    const currentIndex = this.phases.findIndex((p) => p.id === this.status!.current_phase);
    const phaseIndex = this.phases.findIndex((p) => p.id === phaseId);

    if (phaseIndex < currentIndex) return 'completed';
    if (phaseIndex === currentIndex) return 'current';
    return 'pending';
  }

  getStatusIcon(jobStatus: string): string {
    switch (jobStatus) {
      case 'completed':
        return 'check_circle';
      case 'failed':
        return 'error';
      case 'running':
        return 'play_circle';
      default:
        return 'hourglass_empty';
    }
  }

  getStatusColor(jobStatus: string): string {
    switch (jobStatus) {
      case 'completed':
        return 'primary';
      case 'failed':
        return 'warn';
      default:
        return 'accent';
    }
  }
}
