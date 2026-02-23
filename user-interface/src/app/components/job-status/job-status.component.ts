import { Component, Input, OnDestroy, OnInit, output } from '@angular/core';
import { timer, Subscription, switchMap } from 'rxjs';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatExpansionModule } from '@angular/material/expansion';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import type { JobStatusResponse } from '../../models';

@Component({
  selector: 'app-job-status',
  standalone: true,
  imports: [MatCardModule, MatProgressBarModule, MatExpansionModule],
  templateUrl: './job-status.component.html',
  styleUrl: './job-status.component.scss',
})
export class JobStatusComponent implements OnInit, OnDestroy {
  @Input() jobId: string | null = null;

  readonly statusChange = output<JobStatusResponse>();

  status: JobStatusResponse | null = null;
  loading = true;
  private sub: Subscription | null = null;

  constructor(private readonly api: SoftwareEngineeringApiService) {}

  ngOnInit(): void {
    if (this.jobId) {
      this.sub = timer(0, 60000)
        .pipe(switchMap(() => this.api.getJobStatus(this.jobId!)))
        .subscribe({
          next: (res) => {
            this.status = res;
            this.statusChange.emit(res);
            this.loading = false;
            if (res.status === 'completed' || res.status === 'failed') {
              this.sub?.unsubscribe();
              this.sub = null;
            }
          },
          error: () => {
            this.loading = false;
            this.sub?.unsubscribe();
            this.sub = null;
          },
        });
    } else {
      this.loading = false;
    }
  }

  ngOnDestroy(): void {
    this.sub?.unsubscribe();
  }
}
