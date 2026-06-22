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
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTableModule } from '@angular/material/table';
import { SeMetricsApiService } from '../../../services/se-metrics-api.service';
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
    FormsModule,
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
        this.error.set(err?.error?.detail ?? err?.message ?? 'Failed to load metrics.');
        this.loading.set(false);
      },
    });
  }

  /** Human-readable duration from seconds; `—` when no samples (null). */
  formatDuration(seconds: number | null | undefined): string {
    if (seconds === null || seconds === undefined) return '—';
    if (seconds < 60) return `${Math.round(seconds)} s`;
    if (seconds < 3600) return `${(seconds / 60).toFixed(1)} min`;
    if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} h`;
    return `${(seconds / 86400).toFixed(1)} d`;
  }

  /** Format a 0..1 rate as a percentage string. */
  formatPercent(rate: number | null | undefined): string {
    if (rate === null || rate === undefined) return '—';
    return `${(rate * 100).toFixed(1)}%`;
  }

  /** Format a USD amount. */
  formatUsd(amount: number | null | undefined): string {
    if (amount === null || amount === undefined) return '$0.0000';
    return `$${amount.toFixed(4)}`;
  }
}
