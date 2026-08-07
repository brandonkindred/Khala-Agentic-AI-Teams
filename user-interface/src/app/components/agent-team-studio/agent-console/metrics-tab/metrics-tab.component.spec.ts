import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { Subject, of, throwError } from 'rxjs';
import { MetricsTabComponent } from './metrics-tab.component';
import { SeMetricsApiService } from '../../../../services/se-metrics-api.service';
import type { SeMetrics } from '../../../../models/se-metrics.model';

const mockMetrics: SeMetrics = {
  window_days: 30,
  computed_at: '2026-06-20T00:00:00+00:00',
  deployment_count: 4,
  deployment_frequency_per_day: 0.1333,
  lead_time_seconds_median: 7200,
  lead_time_sample_count: 4,
  merged_count: 8,
  gate_reentry_count: 2,
  change_failure_rate: 0.25,
  mttr_seconds_median: 90,
  crash_resolved_count: 1,
  total_cost_usd: 1.2345,
  cost_by_job: { jobA: 0.5, jobB: 0.7345 },
};

describe('MetricsTabComponent', () => {
  let api: { getMetrics: ReturnType<typeof vi.fn> };

  function build() {
    TestBed.configureTestingModule({
      imports: [MetricsTabComponent, NoopAnimationsModule],
      providers: [{ provide: SeMetricsApiService, useValue: api }],
    });
    return TestBed.createComponent(MetricsTabComponent);
  }

  beforeEach(() => {
    api = { getMetrics: vi.fn().mockReturnValue(of(mockMetrics)) };
  });

  it('loads metrics on init with the default 30-day window', () => {
    const fixture = build();
    fixture.detectChanges();
    expect(api.getMetrics).toHaveBeenCalledWith(30);
    expect(fixture.componentInstance.metrics()).toEqual(mockMetrics);
    expect(fixture.componentInstance.loading()).toBe(false);
  });

  it('reloads when the window changes, and ignores the same window', () => {
    const fixture = build();
    fixture.detectChanges();
    fixture.componentInstance.setWindow(7);
    expect(api.getMetrics).toHaveBeenLastCalledWith(7);
    expect(fixture.componentInstance.windowDays()).toBe(7);

    const callCount = api.getMetrics.mock.calls.length;
    fixture.componentInstance.setWindow(7); // no-op
    expect(api.getMetrics.mock.calls.length).toBe(callCount);
  });

  it('cancels an in-flight request on rapid window change (no stale overwrite)', () => {
    // First window's response never arrives; switching window cancels it so a
    // late first-response can't overwrite the newer window's data.
    const first$ = new Subject<SeMetrics>();
    const second = { ...mockMetrics, deployment_count: 99 };
    api.getMetrics = vi
      .fn()
      .mockReturnValueOnce(first$)
      .mockReturnValueOnce(of(second));
    const fixture = build();
    fixture.detectChanges(); // init → subscribes to first$ (pending)
    fixture.componentInstance.setWindow(7); // cancels first$, takes `second`
    expect(fixture.componentInstance.metrics()).toEqual(second);
    expect(first$.observed).toBe(false); // first subscription was torn down
    // A late first-window emission must not overwrite the newer result.
    first$.next({ ...mockMetrics, deployment_count: 1 });
    expect(fixture.componentInstance.metrics()).toEqual(second);
  });

  it('unsubscribes on destroy', () => {
    const pending$ = new Subject<SeMetrics>();
    api.getMetrics = vi.fn().mockReturnValue(pending$);
    const fixture = build();
    fixture.detectChanges();
    expect(pending$.observed).toBe(true);
    fixture.destroy();
    expect(pending$.observed).toBe(false);
  });

  it('surfaces API errors', () => {
    api.getMetrics = vi.fn().mockReturnValue(throwError(() => ({ message: 'boom' })));
    const fixture = build();
    fixture.detectChanges();
    expect(fixture.componentInstance.error()).toBe('boom');
    expect(fixture.componentInstance.loading()).toBe(false);
  });

  it('sorts per-job cost rows by spend descending', () => {
    const fixture = build();
    fixture.detectChanges();
    const rows = fixture.componentInstance.jobCosts();
    expect(rows.map((r) => r.job)).toEqual(['jobB', 'jobA']);
  });

  it('formats durations across magnitudes and null', () => {
    const fixture = build();
    const c = fixture.componentInstance;
    expect(c.formatDuration(null)).toBe('—');
    expect(c.formatDuration(undefined)).toBe('—');
    expect(c.formatDuration(-30)).toBe('—'); // negative is treated as no sample
    expect(c.formatDuration(30)).toBe('30 s');
    expect(c.formatDuration(120)).toBe('2.0 min');
    expect(c.formatDuration(7200)).toBe('2.0 h');
    expect(c.formatDuration(172800)).toBe('2.0 d');
    // Boundary: just under an hour rounds up consistently to the higher unit.
    expect(c.formatDuration(3599.9)).toBe('1.0 h');
  });

  it('formats percent and usd', () => {
    const fixture = build();
    const c = fixture.componentInstance;
    expect(c.formatPercent(0.25)).toBe('25.0%');
    expect(c.formatPercent(null)).toBe('—');
    expect(c.formatUsd(1.2345)).toBe('$1.2345');
    expect(c.formatUsd(0)).toBe('$0.0000');
    expect(c.formatUsd(null)).toBe('—');
    expect(c.formatUsd(undefined)).toBe('—');
  });

  it('extracts a string error body', () => {
    api.getMetrics = vi.fn().mockReturnValue(throwError(() => ({ error: 'string error' })));
    const fixture = build();
    fixture.detectChanges();
    expect(fixture.componentInstance.error()).toBe('string error');
  });

  it('extracts error.detail from an object error body', () => {
    api.getMetrics = vi
      .fn()
      .mockReturnValue(throwError(() => ({ error: { detail: 'detail error' } })));
    const fixture = build();
    fixture.detectChanges();
    expect(fixture.componentInstance.error()).toBe('detail error');
  });

  it('falls back to a generic message when the error has no recognizable shape', () => {
    api.getMetrics = vi.fn().mockReturnValue(throwError(() => ({})));
    const fixture = build();
    fixture.detectChanges();
    expect(fixture.componentInstance.error()).toBe('Failed to load metrics.');
  });

  it('formats timestamps (valid, invalid, null)', () => {
    const fixture = build();
    const c = fixture.componentInstance;
    expect(c.formatTimestamp(null)).toBe('—');
    expect(c.formatTimestamp(undefined)).toBe('—');
    expect(c.formatTimestamp('not-a-date')).toBe('not-a-date');
    const formatted = c.formatTimestamp('2026-06-20T00:00:00+00:00');
    expect(typeof formatted).toBe('string');
    expect(formatted).not.toBe('—');
  });
});
