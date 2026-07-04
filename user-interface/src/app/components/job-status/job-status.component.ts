import { Component, Input, OnDestroy, OnInit, output, inject } from '@angular/core';
import { timer, Subscription, switchMap } from 'rxjs';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatIconModule } from '@angular/material/icon';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import type { JobStatusResponse } from '../../models';
import { markStatusReceived } from '../../shared/staleness.util';
import { isCodingTeamTerminalStatus } from '../../models/job-status.model';
import { StallWarningComponent } from '../../shared/stall-warning/stall-warning.component';
import { InlineBannerComponent } from '../../shared/inline-banner/inline-banner.component';

@Component({
  selector: 'app-job-status',
  standalone: true,
  imports: [MatCardModule, MatProgressBarModule, MatExpansionModule, MatIconModule, StallWarningComponent, InlineBannerComponent],
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
          // Receipt stamp turns the response's server_time into a clock offset,
          // so staleness ages advance between polls instead of freezing on the
          // last snapshot (see staleness.util.ts).
          this.status = markStatusReceived(res);
          this.statusChange.emit(res);
          this.loading = false;
          // Stop polling on any coding-team terminal status. Routed through the shared helper
          // (rather than a hard-coded list) so terminal successes like completed_with_failures and
          // already_complete are always recognized here — otherwise the poll runs forever on a job
          // that delegated to the coding team and finished with one of those statuses.
          if (isCodingTeamTerminalStatus(res.status)) {
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
