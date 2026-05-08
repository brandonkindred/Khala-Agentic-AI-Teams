import { Component, Input, OnInit } from '@angular/core';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { Observable, of } from 'rxjs';
import { catchError, map } from 'rxjs/operators';

/**
 * Displays API health status (ok / error) with a visual indicator.
 * Accepts an Observable that should emit when health check completes.
 */
@Component({
  selector: 'app-health-indicator',
  standalone: true,
  imports: [MatIconModule, MatTooltipModule],
  templateUrl: './health-indicator.component.html',
  styleUrl: './health-indicator.component.scss',
})
export class HealthIndicatorComponent implements OnInit {
  /** Observable that performs the health check. Should emit { status: 'ok' } on success. */
  @Input() healthCheck!: () => Observable<{ status?: string }>;

  /** Label for the tooltip (e.g. "Blogging API"). */
  @Input() label = 'API';

  status: 'ok' | 'error' | 'checking' = 'checking';

  ngOnInit(): void {
    if (this.healthCheck) {
      this.healthCheck()
        .pipe(
          map((r) => {
            const s = (r?.status ?? '').toLowerCase();
            return s === 'ok' || s === 'healthy' ? 'ok' : 'error';
          }),
          catchError(() => of('error'))
        )
        .subscribe((s) => {
          this.status = s as 'ok' | 'error';
        });
    } else {
      this.status = 'error';
    }
  }
}
