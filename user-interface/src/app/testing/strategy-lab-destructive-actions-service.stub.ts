import { signal } from '@angular/core';
import { Subject } from 'rxjs';
import { vi } from 'vitest';

/**
 * A `StrategyLabDestructiveActionsService` test double: real `clearingAll`/
 * `deletingLabRecordId` signals (so components read them exactly as they
 * would the live service) plus `Subject`s backing `errors$` and
 * `resultsRefreshRequested$`, with `vi.fn()` action methods a test can
 * assert against, mirroring `createPaperTradingServiceStub()`'s shape in
 * `strategy-lab-paper-trading-service.stub.ts`.
 *
 * Preconditions: none — call once per test (or per fixture) needing an
 *   isolated `StrategyLabDestructiveActionsService` stand-in; each call
 *   returns fresh signals/Subjects/`vi.fn()`s, never shared state across
 *   calls.
 * Postconditions: returns an object whose `clearingAll` signal starts
 *   `false` and whose `deletingLabRecordId` signal starts `null` (matching
 *   the real service's constructor), and whose `errors$`/
 *   `resultsRefreshRequested$` are fresh `Subject`s a test can `.next()`
 *   into to simulate the real service's forwarding.
 */
export function createDestructiveActionsServiceStub() {
  const clearingAll = signal(false);
  const deletingLabRecordId = signal<string | null>(null);
  const errors$ = new Subject<string | null>();
  const resultsRefreshRequested$ = new Subject<void>();
  return {
    clearingAll,
    deletingLabRecordId,
    errors$,
    resultsRefreshRequested$,
    deleteRecord: vi.fn(),
    clearAllLabData: vi.fn(),
  };
}

export type DestructiveActionsServiceStub = ReturnType<typeof createDestructiveActionsServiceStub>;
