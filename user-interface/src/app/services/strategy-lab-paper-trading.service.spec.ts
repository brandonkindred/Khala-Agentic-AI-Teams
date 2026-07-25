import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { StrategyLabPaperTradingService } from './strategy-lab-paper-trading.service';
import { StrategyLabRunService } from './strategy-lab-run.service';
import { InvestmentApiService } from './investment-api.service';
import { createRunServiceStub, type RunServiceStub } from '../testing/strategy-lab-run-service.stub';
import type { StrategyLabRecord } from '../models';

describe('StrategyLabPaperTradingService', () => {
  let service: StrategyLabPaperTradingService;
  let runService: RunServiceStub;
  let apiSpy: {
    getPaperTradingResults: ReturnType<typeof vi.fn>;
    runPaperTrading: ReturnType<typeof vi.fn>;
  };

  const publishableRecord: StrategyLabRecord = {
    lab_record_id: 'rec-1',
    is_winning: true,
    is_publishable: true,
    strategy_rationale: '',
    analysis_narrative: '',
    created_at: '',
    strategy: {} as never,
    backtest: {} as never,
  };

  const unpublishableRecord: StrategyLabRecord = {
    ...publishableRecord,
    lab_record_id: 'rec-legacy',
    is_publishable: false,
    publishability_skip_reason: 'realism_failed',
  };

  beforeEach(() => {
    runService = createRunServiceStub();
    apiSpy = {
      getPaperTradingResults: vi.fn().mockReturnValue(of({ items: [] })),
      runPaperTrading: vi.fn().mockReturnValue(of({ session: { session_id: 'pt-1', status: 'running' } })),
    };
    TestBed.configureTestingModule({
      providers: [
        StrategyLabPaperTradingService,
        { provide: StrategyLabRunService, useValue: runService },
        { provide: InvestmentApiService, useValue: apiSpy },
      ],
    });
    service = TestBed.inject(StrategyLabPaperTradingService);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  it('starts with no in-flight paper trade', () => {
    expect(service.paperTradingLabRecordId()).toBeNull();
  });

  describe('runPaperTrading', () => {
    it('no-ops and emits an error on errors$ when the record is not publishable', () => {
      const messages: (string | null)[] = [];
      service.errors$.subscribe((m) => messages.push(m));

      service.runPaperTrading(unpublishableRecord);

      expect(apiSpy.runPaperTrading).not.toHaveBeenCalled();
      expect(messages).toHaveLength(1);
      expect(messages[0]).toContain('not publishable');
      expect(messages[0]).toContain('realism_failed');
      expect(service.paperTradingLabRecordId()).toBeNull();
    });

    it('emits null on errors$ (clearing any stale error) the instant the publishability guard passes', () => {
      const messages: (string | null)[] = [];
      service.errors$.subscribe((m) => messages.push(m));

      service.runPaperTrading(publishableRecord);

      expect(messages[0]).toBeNull();
    });

    it('tracks the session via runService on success and clears its own in-flight state', () => {
      service.runPaperTrading(publishableRecord);

      expect(apiSpy.runPaperTrading).toHaveBeenCalledWith({ lab_record_id: 'rec-1' });
      expect(runService.trackPaperTradingSession).toHaveBeenCalledWith('rec-1', { session_id: 'pt-1', status: 'running' });
      // The stub's trackPaperTradingSession sets runService's own paperTradingLabRecordId
      // (matching the real service), so the merged signal still reflects 'rec-1' — it's
      // this service's *local* optimistic flag that clears on success.
      expect(service.paperTradingLabRecordId()).toBe('rec-1');
    });

    it('surfaces an error on errors$ and clears the in-flight id on failure', () => {
      apiSpy.runPaperTrading.mockReturnValue(throwError(() => ({ error: { detail: 'worker unavailable' } })));
      const messages: (string | null)[] = [];
      service.errors$.subscribe((m) => messages.push(m));

      service.runPaperTrading(publishableRecord);

      expect(messages).toContain('worker unavailable');
      expect(service.paperTradingLabRecordId()).toBeNull();
    });
  });

  describe('loadPaperTradingResults', () => {
    it('keeps the most recent session per lab record and hydrates runService', () => {
      apiSpy.getPaperTradingResults.mockReturnValue(
        of({
          items: [
            { lab_record_id: 'rec-1', session_id: 'old', status: 'completed', started_at: '2026-01-01T00:00:00Z' },
            { lab_record_id: 'rec-1', session_id: 'new', status: 'running', started_at: '2026-01-02T00:00:00Z' },
            { lab_record_id: 'rec-2', session_id: 'other', status: 'completed', started_at: '2026-01-01T00:00:00Z' },
          ],
        }),
      );

      service.loadPaperTradingResults();

      expect(runService.hydratePaperTradingSessions).toHaveBeenCalledWith({
        'rec-1': { lab_record_id: 'rec-1', session_id: 'new', status: 'running', started_at: '2026-01-02T00:00:00Z' },
        'rec-2': { lab_record_id: 'rec-2', session_id: 'other', status: 'completed', started_at: '2026-01-01T00:00:00Z' },
      });
    });
  });

  describe('getPaperSession', () => {
    it('reads the session from runService', () => {
      runService.paperTradingSessions.set({ 'rec-1': { session_id: 'pt-1', status: 'running' } as never });
      expect(service.getPaperSession(publishableRecord)).toEqual({ session_id: 'pt-1', status: 'running' });
      expect(service.getPaperSession(unpublishableRecord)).toBeNull();
    });
  });
});
