import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { Subject, of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { LlmUsageApiService } from '../../services/llm-usage-api.service';
import { LlmUsageDashboardComponent } from './llm-usage-dashboard.component';
import type { LlmUsageCall, LlmUsageSummary } from '../../models/llm-usage.model';

function summary(over: Partial<LlmUsageSummary> = {}): LlmUsageSummary {
  return {
    team: 'all',
    window: '24h',
    window_hours: 24,
    total_calls: 2,
    total_prompt_tokens: 30,
    total_completion_tokens: 10,
    total_tokens: 40,
    avg_latency_ms: 0,
    error_count: 0,
    by_agent: {},
    by_model: {
      'claude-opus-4-8': { calls: 2, prompt_tokens: 30, completion_tokens: 10, total_tokens: 40 },
    },
    storage_available: true,
    storage_status: 'available',
    ...over,
  };
}

function callRow(over: Partial<LlmUsageCall> = {}): LlmUsageCall {
  return {
    timestamp: 1_700_000_000,
    team: 'blogging',
    agent_key: 'writer',
    model: 'claude-opus-4-8',
    prompt_tokens: 10,
    completion_tokens: 5,
    total_tokens: 15,
    status: 'success',
    ...over,
  };
}

describe('LlmUsageDashboardComponent', () => {
  let component: LlmUsageDashboardComponent;
  let fixture: ComponentFixture<LlmUsageDashboardComponent>;
  let apiSpy: { getSummary: ReturnType<typeof vi.fn>; getRecent: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    apiSpy = {
      getSummary: vi.fn().mockReturnValue(of(summary())),
      getRecent: vi.fn().mockReturnValue(of([callRow()])),
    };
    await TestBed.configureTestingModule({
      imports: [LlmUsageDashboardComponent, NoopAnimationsModule],
      providers: [{ provide: LlmUsageApiService, useValue: apiSpy }],
    }).compileComponents();
    fixture = TestBed.createComponent(LlmUsageDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('loads 24h summary and recent on init', () => {
    expect(apiSpy.getSummary).toHaveBeenCalledWith('24h');
    expect(apiSpy.getRecent).toHaveBeenCalledWith('24h', 100);
    expect(component.summary.total_calls).toBe(2);
    expect(component.summary.total_prompt_tokens).toBe(30);
    expect(component.summary.total_completion_tokens).toBe(10);
    expect(component.summary.total_tokens).toBe(40);
  });

  it('refetches both endpoints when the window chip changes', () => {
    apiSpy.getSummary.mockClear();
    apiSpy.getRecent.mockClear();
    apiSpy.getSummary.mockReturnValue(of(summary({ window: '7d', window_hours: 168 })));
    apiSpy.getRecent.mockReturnValue(of([]));
    component.setWindow('7d');
    expect(apiSpy.getSummary).toHaveBeenCalledWith('7d');
    expect(apiSpy.getRecent).toHaveBeenCalledWith('7d', 100);
  });

  it('a slow earlier forkJoin cannot clobber a later window', () => {
    const slow24h$ = new Subject<LlmUsageSummary>();
    const sevenDay = summary({
      window: '7d',
      window_hours: 168,
      total_calls: 7,
      total_prompt_tokens: 70,
      total_completion_tokens: 21,
      total_tokens: 91,
    });

    apiSpy.getSummary.mockClear();
    apiSpy.getRecent.mockClear();
    apiSpy.getSummary
      .mockReturnValueOnce(slow24h$.asObservable())
      .mockReturnValueOnce(of(sevenDay));
    apiSpy.getRecent.mockReturnValue(of([]));

    component.setWindow('24h');
    component.setWindow('7d');

    slow24h$.next(
      summary({
        window: '24h',
        total_calls: 999,
        total_prompt_tokens: 9000,
        total_completion_tokens: 900,
        total_tokens: 9900,
      }),
    );
    slow24h$.complete();

    expect(component.window).toBe('7d');
    expect(component.summary.window).toBe('7d');
    expect(component.summary.total_calls).toBe(7);
    expect(component.summary.total_tokens).toBe(91);
  });

  it('labels empty recent-call models as (unknown)', () => {
    apiSpy.getSummary.mockReturnValue(of(summary()));
    apiSpy.getRecent.mockReturnValue(of([callRow({ model: '' })]));
    component.load();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('(unknown)');
  });

  it('shows the per-model table when a single model has data', () => {
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('[data-testid="by-model-table"]')).not.toBeNull();
    expect(el.textContent).toContain('claude-opus-4-8');
  });

  it('shows empty state when totals are zero', () => {
    apiSpy.getSummary.mockReturnValue(
      of(summary({ total_calls: 0, total_tokens: 0, by_model: {} })),
    );
    apiSpy.getRecent.mockReturnValue(of([]));
    component.load();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toMatch(/No LLM calls/i);
  });

  it('shows a storage banner when Postgres is unavailable', () => {
    apiSpy.getSummary.mockReturnValue(
      of(summary({ storage_available: false, storage_status: 'unconfigured', total_calls: 9 })),
    );
    component.load();
    fixture.detectChanges();
    expect(component.storageAvailable).toBe(false);
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('app-inline-banner')).not.toBeNull();
    expect(component.displaySummary.total_calls).toBe(0);
  });

  it('shows an error banner when the load fails', () => {
    apiSpy.getSummary.mockReturnValue(throwError(() => ({ error: { detail: 'usage down' } })));
    component.load();
    fixture.detectChanges();
    expect(component.loadError).toBe('usage down');
    expect(component.summary.total_calls).toBe(0);
    expect(component.recent).toEqual([]);
  });

  it('does not keep the previous window totals when a refetch fails', () => {
    apiSpy.getSummary.mockReturnValue(throwError(() => ({ error: { detail: '7d failed' } })));
    apiSpy.getRecent.mockReturnValue(throwError(() => ({ error: { detail: '7d failed' } })));
    component.setWindow('7d');
    fixture.detectChanges();
    expect(component.window).toBe('7d');
    expect(component.summary.window).toBe('7d');
    expect(component.summary.total_calls).toBe(0);
    expect(component.summary.total_tokens).toBe(0);
    expect(component.recent).toEqual([]);
    expect(component.loadError).toBe('7d failed');
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('[data-testid="by-model-table"]')).toBeNull();
    expect(el.textContent).not.toMatch(/No LLM calls/i);
  });
});
