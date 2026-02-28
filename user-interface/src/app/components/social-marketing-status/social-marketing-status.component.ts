import { Component, Input, OnDestroy, OnInit } from '@angular/core';
import { timer, Subscription, switchMap } from 'rxjs';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { SocialMarketingApiService } from '../../services/social-marketing-api.service';
import type { MarketingJobStatusResponse } from '../../models';

@Component({
  selector: 'app-social-marketing-status',
  standalone: true,
  imports: [MatCardModule, MatProgressBarModule],
  templateUrl: './social-marketing-status.component.html',
  styleUrl: './social-marketing-status.component.scss',
})
export class SocialMarketingStatusComponent implements OnInit, OnDestroy {
  @Input() jobId: string | null = null;

  status: MarketingJobStatusResponse | null = null;
  loading = true;
  private sub: Subscription | null = null;

  constructor(private readonly api: SocialMarketingApiService) {}

  ngOnInit(): void {
    if (this.jobId) {
      this.sub = timer(0, 60000)
        .pipe(switchMap(() => this.api.getStatus(this.jobId!)))
        .subscribe({
          next: (res) => {
            this.status = res;
            this.loading = false;
            if (res.status === 'completed' || res.status === 'failed' || res.status === 'cancelled') {
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
