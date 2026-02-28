import { Component, Input, OnDestroy, OnInit } from '@angular/core';
import { timer, Subscription, switchMap } from 'rxjs';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatExpansionModule } from '@angular/material/expansion';
import { Soc2ComplianceApiService } from '../../services/soc2-compliance-api.service';
import type { AuditStatusResponse } from '../../models';

/**
 * Polls GET /soc2-audit/status/{job_id} and displays status and result.
 */
@Component({
  selector: 'app-soc2-audit-status',
  standalone: true,
  imports: [MatCardModule, MatProgressBarModule, MatExpansionModule],
  templateUrl: './soc2-audit-status.component.html',
  styleUrl: './soc2-audit-status.component.scss',
})
export class Soc2AuditStatusComponent implements OnInit, OnDestroy {
  @Input() jobId: string | null = null;

  status: AuditStatusResponse | null = null;
  loading = true;
  private sub: Subscription | null = null;

  constructor(private readonly api: Soc2ComplianceApiService) {}

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
