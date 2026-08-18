import { Component, DestroyRef, OnInit, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { EMPTY, Subject, catchError, forkJoin, switchMap } from 'rxjs';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { LlmUsageApiService } from '../../services/llm-usage-api.service';
import { extractErrorDetail } from '../../core/error-handler.interceptor';
import { InlineBannerComponent } from '../../shared/inline-banner/inline-banner.component';
import type {
  LlmUsageCall,
  LlmUsageModelBreakdown,
  LlmUsageStorageStatus,
  LlmUsageSummary,
  LlmUsageWindow,
} from '../../models/llm-usage.model';

const EMPTY_TOTALS: Pick<
  LlmUsageSummary,
  | 'total_calls'
  | 'total_prompt_tokens'
  | 'total_completion_tokens'
  | 'total_tokens'
  | 'total_cache_read_tokens'
  | 'total_cache_creation_tokens'
  | 'by_model'
  | 'by_agent'
  | 'error_count'
  | 'avg_latency_ms'
> = {
  total_calls: 0,
  total_prompt_tokens: 0,
  total_completion_tokens: 0,
  total_tokens: 0,
  total_cache_read_tokens: 0,
  total_cache_creation_tokens: 0,
  by_model: {},
  by_agent: {},
  error_count: 0,
  avg_latency_ms: 0,
};

const WINDOW_HOURS: Record<LlmUsageWindow, number> = {
  '24h': 24,
  '7d': 168,
  '30d': 720,
  all: 0,
};

function emptySummary(window: LlmUsageWindow = '24h'): LlmUsageSummary {
  return {
    team: 'all',
    window,
    window_hours: WINDOW_HOURS[window],
    ...EMPTY_TOTALS,
    storage_available: true,
    storage_status: 'available',
  };
}

/**
 * Settings page for durable LLM request/token totals.
 *
 * Preconditions: `LlmUsageApiService` is injectable.
 * Postconditions: loads summary + recent for the selected window; zeros
 * displayed totals when storage is unavailable while still showing the banner.
 */
@Component({
  selector: 'app-llm-usage-dashboard',
  standalone: true,
  imports: [
    MatButtonToggleModule,
    MatCardModule,
    MatIconModule,
    MatProgressSpinnerModule,
    InlineBannerComponent,
  ],
  templateUrl: './llm-usage-dashboard.component.html',
  styleUrl: './llm-usage-dashboard.component.scss',
})
export class LlmUsageDashboardComponent implements OnInit {
  private readonly api = inject(LlmUsageApiService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly windowLoad$ = new Subject<LlmUsageWindow>();

  window: LlmUsageWindow = '24h';
  readonly windows: { id: LlmUsageWindow; label: string }[] = [
    { id: '24h', label: '24h' },
    { id: '7d', label: '7d' },
    { id: '30d', label: '30d' },
    { id: 'all', label: 'All time' },
  ];

  summary: LlmUsageSummary = emptySummary();
  recent: LlmUsageCall[] = [];
  storageStatus: LlmUsageStorageStatus = 'available';
  loadError: string | null = null;
  loading = false;

  get storageAvailable(): boolean {
    return this.summary.storage_available;
  }

  /**
   * Recent-call rows for the table, newest first.
   *
   * Preconditions: none.
   * Postconditions: returns `recent` reversed; the API list stays
   * oldest-to-newest (most recent last).
   */
  get displayRecent(): LlmUsageCall[] {
    return this.recent.slice().reverse();
  }

  /**
   * Summary shown in cards/tables. When storage is unavailable, totals are
   * zeroed so the page never presents the in-memory buffer as durable history.
   *
   * Preconditions: none.
   * Postconditions: returns a summary whose `storage_*` fields match the last
   * response; totals are zero when `!storageAvailable`.
   */
  get displaySummary(): LlmUsageSummary {
    if (!this.storageAvailable) {
      return { ...this.summary, ...EMPTY_TOTALS };
    }
    return this.summary;
  }

  ngOnInit(): void {
    this.windowLoad$
      .pipe(
        switchMap((window) =>
          forkJoin({
            summary: this.api.getSummary(window),
            recent: this.api.getRecent(window, 100),
          }).pipe(
            catchError((err) => {
              this.loadError = extractErrorDetail(err, 'Failed to load LLM usage.');
              this.loading = false;
              const previous = this.summary;
              this.summary = {
                ...emptySummary(this.window),
                storage_available: previous.storage_available,
                storage_status: previous.storage_status,
              };
              this.recent = [];
              return EMPTY;
            }),
          ),
        ),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe(({ summary, recent }) => {
        this.summary = summary;
        this.recent = recent;
        this.storageStatus = summary.storage_status;
        this.loadError = null;
        this.loading = false;
      });
    this.load();
  }

  /**
   * Fetch summary and recent calls for the current window.
   *
   * Preconditions: none.
   * Postconditions: on success, sets `summary`, `recent`, `storageStatus` and
   * clears `loadError`; on failure, sets `loadError` via `extractErrorDetail`,
   * zeros `summary`/`recent` for the selected window (so a failed refetch
   * cannot keep the previous window's totals), and leaves `storage_*` from the
   * last successful response.
   * A newer `load()` cancels any in-flight pair via `switchMap`.
   */
  load(): void {
    this.loading = true;
    this.loadError = null;
    this.windowLoad$.next(this.window);
  }

  /**
   * Switch the time window and refetch both endpoints.
   *
   * Preconditions: `w` is a valid `LlmUsageWindow`.
   * Postconditions: `this.window` equals `w` and `load()` has been invoked.
   */
  setWindow(w: LlmUsageWindow): void {
    this.window = w;
    this.load();
  }

  /**
   * Flatten `displaySummary.by_model` into table rows.
   *
   * Preconditions: none.
   * Postconditions: one row per model key; empty model keys become `(unknown)`.
   */
  modelRows(): ({ model: string } & LlmUsageModelBreakdown)[] {
    return Object.entries(this.displaySummary.by_model).map(([model, b]) => ({
      model: model || '(unknown)',
      ...b,
    }));
  }

  /**
   * Format a unix-seconds timestamp for the recent table.
   *
   * Preconditions: `ts` is unix seconds (not milliseconds).
   * Postconditions: returns a locale datetime string.
   */
  formatTime(ts: number): string {
    return new Date(ts * 1000).toLocaleString();
  }
}
