import { signal } from '@angular/core';
import { Subject } from 'rxjs';
import { vi } from 'vitest';
import type { StrategyLabRecord } from '../models';

/**
 * A `StrategyLabPaperTradingService` test double: a real
 * `paperTradingLabRecordId` signal (so components read it exactly as they
 * would the live service) plus a `Subject` backing `errors$`, with
 * `vi.fn()` action methods a test can assert against or drive manually.
 *
 * Shared by `strategy-lab.component.spec.ts`,
 * `strategy-lab.component.integration.spec.ts`, and
 * `strategy-lab.component.a11y.spec.ts`, mirroring
 * `createRunServiceStub()`'s rationale in `strategy-lab-run-service.stub.ts`.
 *
 * Preconditions: none — call once per test (or per fixture) needing an
 *   isolated `StrategyLabPaperTradingService` stand-in; each call returns
 *   fresh signals/Subjects/`vi.fn()`s, never shared state across calls.
 * Postconditions: returns an object whose `paperTradingLabRecordId` signal
 *   starts `null` (matching the real service's constructor), and whose
 *   `errors$` is a fresh `Subject` a test can `.next()` into to simulate the
 *   real service's error forwarding.
 */
export function createPaperTradingServiceStub() {
  const paperTradingLabRecordId = signal<string | null>(null);
  const errors$ = new Subject<string | null>();
  return {
    paperTradingLabRecordId,
    errors$,
    loadPaperTradingResults: vi.fn(),
    runPaperTrading: vi.fn((record: StrategyLabRecord) => {
      paperTradingLabRecordId.set(record.lab_record_id);
    }),
    getPaperSession: vi.fn(() => null),
  };
}

export type PaperTradingServiceStub = ReturnType<typeof createPaperTradingServiceStub>;
