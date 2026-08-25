import { TestBed } from '@angular/core/testing';
import { MatDialog } from '@angular/material/dialog';
import { Subject, of, throwError } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { StrategyLabDestructiveActionsService } from './strategy-lab-destructive-actions.service';
import { StrategyLabRunService } from './strategy-lab-run.service';
import { InvestmentApiService } from './investment-api.service';
import { NotificationService } from '../core/notification.service';
import { ConfirmDestructiveService } from '../shared/confirm-destructive.service';
import { createRunServiceStub, type RunServiceStub } from '../testing/strategy-lab-run-service.stub';
import type { StrategyLabRecord } from '../models';

describe('StrategyLabDestructiveActionsService', () => {
  let service: StrategyLabDestructiveActionsService;
  let runService: RunServiceStub;
  let apiSpy: {
    deleteStrategyLabRecord: ReturnType<typeof vi.fn>;
    clearStrategyLabStorage: ReturnType<typeof vi.fn>;
  };
  let notifySpy: { saved: ReturnType<typeof vi.fn> };
  let dialogSpy: { open: ReturnType<typeof vi.fn> };
  // Read lazily by the dialog stub's afterClosed(), so a test can set the
  // outcome before invoking the action under test.
  let confirmResult: boolean;

  const record = {
    lab_record_id: 'rec-1',
    strategy: { hypothesis: 'Buy dips in a strong uptrend' },
  } as unknown as StrategyLabRecord;

  beforeEach(() => {
    confirmResult = true;
    runService = createRunServiceStub();
    apiSpy = {
      deleteStrategyLabRecord: vi.fn().mockReturnValue(of({})),
      clearStrategyLabStorage: vi.fn().mockReturnValue(of({})),
    };
    notifySpy = { saved: vi.fn() };
    dialogSpy = {
      open: vi.fn().mockReturnValue({ afterClosed: () => of(confirmResult) }),
    };

    TestBed.configureTestingModule({
      providers: [
        StrategyLabDestructiveActionsService,
        ConfirmDestructiveService,
        { provide: StrategyLabRunService, useValue: runService },
        { provide: InvestmentApiService, useValue: apiSpy },
        { provide: NotificationService, useValue: notifySpy },
        { provide: MatDialog, useValue: dialogSpy },
      ],
    });
    service = TestBed.inject(StrategyLabDestructiveActionsService);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  describe('deleteRecord', () => {
    it('opens a danger confirm dialog and deletes when confirmed', () => {
      confirmResult = true;
      const refreshes: void[] = [];
      service.resultsRefreshRequested$.subscribe(() => refreshes.push(undefined));

      service.deleteRecord(record);

      expect(dialogSpy.open).toHaveBeenCalledTimes(1);
      expect(dialogSpy.open.mock.calls[0][1].data).toMatchObject({ variant: 'danger' });
      expect(apiSpy.deleteStrategyLabRecord).toHaveBeenCalledWith('rec-1', expect.anything());
      expect(refreshes).toHaveLength(1);
      expect(notifySpy.saved).toHaveBeenCalledWith('Strategy lab run deleted.');
      expect(service.deletingLabRecordId()).toBeNull();
    });

    it('opens the confirm dialog without throwing when strategy.hypothesis is missing', () => {
      confirmResult = true;
      const recordWithoutHypothesis = {
        lab_record_id: 'rec-2',
        strategy: undefined,
      } as unknown as StrategyLabRecord;

      expect(() => service.deleteRecord(recordWithoutHypothesis)).not.toThrow();

      expect(dialogSpy.open).toHaveBeenCalledTimes(1);
      const message = dialogSpy.open.mock.calls[0][1].data.message as string;
      expect(message).not.toContain('undefined');
      expect(message).toContain('Delete this strategy lab run?\n\n\n\nThis removes the record');
      expect(apiSpy.deleteStrategyLabRecord).toHaveBeenCalledWith('rec-2', expect.anything());
    });

    it('does not delete when the dialog is cancelled', () => {
      confirmResult = false;

      service.deleteRecord(record);

      expect(dialogSpy.open).toHaveBeenCalledTimes(1);
      expect(apiSpy.deleteStrategyLabRecord).not.toHaveBeenCalled();
      expect(notifySpy.saved).not.toHaveBeenCalled();
    });

    it('surfaces an error on errors$ and skips the toast when delete fails after confirm', () => {
      confirmResult = true;
      apiSpy.deleteStrategyLabRecord.mockReturnValueOnce(
        throwError(() => ({ error: { detail: 'boom' } })),
      );
      const messages: (string | null)[] = [];
      service.errors$.subscribe((m) => messages.push(m));

      service.deleteRecord(record);

      expect(messages).toContain('boom');
      expect(service.deletingLabRecordId()).toBeNull();
      expect(notifySpy.saved).not.toHaveBeenCalled();
    });
  });

  describe('clearAllLabData', () => {
    it('opens a danger confirm dialog and clears all data when confirmed', () => {
      confirmResult = true;
      const refreshes: void[] = [];
      service.resultsRefreshRequested$.subscribe(() => refreshes.push(undefined));

      service.clearAllLabData();

      expect(dialogSpy.open).toHaveBeenCalledTimes(1);
      expect(dialogSpy.open.mock.calls[0][1].data).toMatchObject({ variant: 'danger' });
      expect(apiSpy.clearStrategyLabStorage).toHaveBeenCalled();
      expect(refreshes).toHaveLength(1);
      expect(runService.clearPaperTradingSessions).toHaveBeenCalled();
      expect(notifySpy.saved).toHaveBeenCalledWith('Strategy lab data cleared.');
      expect(service.clearingAll()).toBe(false);
    });

    it('does not clear data when the dialog is cancelled', () => {
      confirmResult = false;

      service.clearAllLabData();

      expect(dialogSpy.open).toHaveBeenCalledTimes(1);
      expect(apiSpy.clearStrategyLabStorage).not.toHaveBeenCalled();
      expect(notifySpy.saved).not.toHaveBeenCalled();
    });

    it('surfaces an error on errors$ and skips the toast when clear-all fails after confirm', () => {
      confirmResult = true;
      apiSpy.clearStrategyLabStorage.mockReturnValueOnce(
        throwError(() => ({ error: { detail: 'kaboom' } })),
      );
      const messages: (string | null)[] = [];
      service.errors$.subscribe((m) => messages.push(m));

      service.clearAllLabData();

      expect(messages).toContain('kaboom');
      expect(service.clearingAll()).toBe(false);
      expect(notifySpy.saved).not.toHaveBeenCalled();
    });
  });

  it('does not open a second confirmation while one dialog is still pending', () => {
    // A dialog that has not resolved yet (afterClosed has not emitted), so the
    // re-entrancy guard should stay engaged across a rapid second activation.
    const closed$ = new Subject<boolean>();
    dialogSpy.open.mockReturnValue({ afterClosed: () => closed$.asObservable() });

    service.deleteRecord(record);
    service.deleteRecord(record); // rapid second activation before the first closes

    expect(dialogSpy.open).toHaveBeenCalledTimes(1);
    expect(apiSpy.deleteStrategyLabRecord).not.toHaveBeenCalled();

    // Closing the first dialog releases the guard so later actions work again.
    closed$.next(false);
    closed$.complete();
    service.clearAllLabData();
    expect(dialogSpy.open).toHaveBeenCalledTimes(2);
  });
});
