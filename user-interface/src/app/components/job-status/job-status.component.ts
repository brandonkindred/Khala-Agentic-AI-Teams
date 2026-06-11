import { Component, Input, OnDestroy, OnInit, output, inject } from '@angular/core';
import { timer, Subscription, switchMap } from 'rxjs';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatIconModule } from '@angular/material/icon';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import type { JobStatusResponse } from '../../models';
import { StallWarningComponent } from '../../shared/stall-warning/stall-warning.component';

@Component({
  selector: 'app-job-status',
  standalone: true,
  imports: [MatCardModule, MatProgressBarModule, MatExpansionModule, MatIconModule, StallWarningComponent],
  templateUrl: './job-status.component.html',
  styleUrl: './job-status.component.scss',
})
export class JobStatusComponent implements OnInit, OnDestroy {
  private readonly api = inject(SoftwareEngineeringApiService);

  @Input() jobId: string | null = null;

  readonly statusChange = output<JobStatusResponse>();

  status: JobStatusResponse | null = null;
  loading = true;
  private sub: Subscription | null = null;

  ngOnInit(): void {
    if (this.jobId) {
      this.startPolling();
    } else {
      this.loading = false;
    }
  }

  private startPolling(): void {
    this.sub?.unsubscribe();
    const pollInterval = this.status?.waiting_for_answers ? 5000 : 15000;
    this.sub = timer(0, pollInterval)
      .pipe(switchMap(() => this.api.getJobStatus(this.jobId!)))
      .subscribe({
        next: (res) => {
          const wasWaiting = this.status?.waiting_for_answers;
          const isWaiting = res.waiting_for_answers;
          this.status = res;
          this.statusChange.emit(res);
          this.loading = false;
          // completed_with_failures is the coding team's partial-success terminal
          // status — without it here the poll runs forever on such jobs.
          if (
            res.status === 'completed' ||
            res.status === 'completed_with_failures' ||
            res.status === 'failed' ||
            res.status === 'cancelled'
          ) {
            this.sub?.unsubscribe();
            this.sub = null;
          } else if (wasWaiting !== isWaiting) {
            this.startPolling();
          }
        },
        error: () => {
          this.loading = false;
          this.sub?.unsubscribe();
          this.sub = null;
        },
      });
  }

  ngOnDestroy(): void {
    this.sub?.unsubscribe();
  }

}
