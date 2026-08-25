import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import { Subscription } from 'rxjs';
import { CommonModule } from '@angular/common';
import { MatButtonModule } from '@angular/material/button';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTableModule } from '@angular/material/table';
import { SeMetricsApiService } from '../../../services/se-metrics-api.service';
import { extractErrorDetail } from '../../../shared/extract-error-detail';
import type { SeMetrics } from '../../../models/se-metrics.model';

/** Selectable lookback windows, in days. */
const WINDOW_OPTIONS = [7, 30, 90] as const;

interface JobCostRow {
  job: string;
  cost: number;
}

/**
 * Agent Console "Metrics" tab: renders the four DORA metrics (deployment
 * frequency, lead time, change-failure rate, MTTR) plus LLM cost for the
 * Software Engineering team, over a selectable window.
 */
@Component({
  selector: 'app-metrics-tab',
  standalone: true,
  imports: [
    CommonModule,
    MatButtonModule,
    MatButtonToggleModule,
    MatCardModule,
    MatIconModule,
    MatProgressSpinnerModule,
    MatTableModule,
  ],
  templateUrl: './metrics-tab.component.html',
  styleUrl: './metrics-tab.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class MetricsTabComponent implements OnInit, OnDestroy {
  private readonly api = inject(SeMetricsApiService);

  /** The in-flight metrics request, if any, so it can be cancelled. */
  private loadSub?: Subscription;

  readonly windowOptions = WINDOW_OPTIONS;
  readonly windowDays = signal<number>(30);
  readonly metrics = signal<SeMetrics | null>(null);
  readonly loading = signal<boolean>(false);
  readonly error = signal<string | null>(null);

  /** Per-job cost rows, highest spend first. */
  readonly jobCosts = computed<JobCostRow[]>(() => {
    const byJob = this.metrics()?.cost_by_job ?? {};
    return Object.entries(byJob)
      .map(([job, cost]) => ({ job, cost }))
      .sort((a, b) => b.cost - a.cost);
  });

  ngOnInit(): void {
    this.load();
  }

  ngOnDestroy(): void {
    this.loadSub?.unsubscribe();
  }

  setWindow(days: number): void {
    if (days === this.windowDays()) return;
    this.windowDays.set(days);
    this.load();
  }

  load(): void {
    // Cancel any in-flight request so a rapid window change can't let a stale,
    // late-arriving response overwrite the newer one (HttpClient aborts the
    // request when its subscription is torn down).
    this.loadSub?.unsubscribe();
    this.loading.set(true);
    this.error.set(null);
    this.loadSub = this.api.getMetrics(this.windowDays()).subscribe({
      next: (m) => {
        this.metrics.set(m);
        this.loading.set(false);
      },
      error: (err) => {
        // ``err.error`` may be a parsed body ({ detail }) or a raw string; fall
        // back to the transport message, then a generic label.
        const detail =
          typeof err?.error === 'string'
            ? err.error
            : extractErrorDetail(err, 'Failed to load metrics.');
        this.error.set(detail);
        this.loading.set(false);
      },
    });
  }

  /** Human-readable duration from seconds; `—` when no samples (null) or negative. */
  formatDuration(seconds: number | null | undefined): string {
    if (seconds === null || seconds === undefined || seconds < 0) return '—';
    // Round first, then test every threshold against the rounded value so the
    // boundary and the displayed value always agree (59.9 s reads as "1.0 min",
    // and 3599.9 s reads as "1.0 h", not "60.0 min").
    const whole = Math.round(seconds);
    if (whole < 60) return `${whole} s`;
    if (whole < 3600) return `${(whole / 60).toFixed(1)} min`;
    if (whole < 86400) return `${(whole / 3600).toFixed(1)} h`;
    return `${(whole / 86400).toFixed(1)} d`;
  }

  /** Format a 0..1 rate as a percentage string. */
  formatPercent(rate: number | null | undefined): string {
    if (rate === null || rate === undefined) return '—';
    return `${(rate * 100).toFixed(1)}%`;
  }

  /** Format a USD amount; `—` when absent (null/undefined), matching the other formatters. */
  formatUsd(amount: number | null | undefined): string {
    if (amount === null || amount === undefined) return '—';
    return `$${amount.toFixed(4)}`;
  }

  /** Render an ISO-8601 timestamp as a localized date/time; the raw value if unparseable. */
  formatTimestamp(iso: string | null | undefined): string {
    if (!iso) return '—';
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
  }
}
