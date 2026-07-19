import { signal } from '@angular/core';
import { Subject } from 'rxjs';
import { vi } from 'vitest';
import type { PaperTradingSession, StrategyLabRunStatus, StrategyLabStreamEvent } from '../models';

/**
 * A `StrategyLabRunService` test double: real signals (so components read
 * them exactly as they would the live service) with `vi.fn()` action methods
 * that apply the same minimal state changes the real service would, so
 * callers observing `running()`/`runStatus()` etc. after calling
 * `startRun()` and friends see realistic results without needing a fake SSE
 * stream or fake timers.
 *
 * Shared by `strategy-lab.component.spec.ts` and
 * `strategy-lab.component.a11y.spec.ts` — previously two independently
 * hand-maintained copies that had to be kept in sync by hand.
 *
 * Preconditions: none — call once per test (or per fixture) needing an
 *   isolated `StrategyLabRunService` stand-in; each call returns fresh
 *   signals/Subjects/`vi.fn()`s, never shared state across calls.
 * Postconditions: returns an object whose signal-typed properties
 *   (`runStatus`, `running`, `activeRunId`, `paperTradingSessions`,
 *   `paperTradingLabRecordId`, `lastTerminalStatus`) start at the same
 *   values `StrategyLabRunService`'s constructor does, and whose action
 *   methods (`startRun`, `clearPaperTradingSessions`,
 *   `hydratePaperTradingSessions`, `trackPaperTradingSession`) mutate those
 *   same signals the way the real service's methods do, so a test driving
 *   the stub observes realistic state transitions without a live SSE
 *   connection.
 */
export function createRunServiceStub() {
  const runStatus = signal<StrategyLabRunStatus | null>(null);
  const running = signal(false);
  const activeRunId = signal<string | null>(null);
  const paperTradingSessions = signal<Record<string, PaperTradingSession>>({});
  const paperTradingLabRecordId = signal<string | null>(null);
  const lastTerminalStatus = signal<StrategyLabRunStatus | null>(null);
  const events$ = new Subject<StrategyLabStreamEvent>();
  const errors$ = new Subject<string>();
  return {
    runStatus,
    running,
    activeRunId,
    paperTradingSessions,
    paperTradingLabRecordId,
    lastTerminalStatus,
    events$,
    errors$,
    checkForActiveRun: vi.fn(),
    startRun: vi.fn((runId: string, status: StrategyLabRunStatus) => {
      lastTerminalStatus.set(null);
      activeRunId.set(runId);
      runStatus.set(status);
      running.set(true);
    }),
    clearPaperTradingSessions: vi.fn(() => paperTradingSessions.set({})),
    hydratePaperTradingSessions: vi.fn((sessions: Record<string, PaperTradingSession>) =>
      paperTradingSessions.set(sessions),
    ),
    trackPaperTradingSession: vi.fn((labRecordId: string, session: PaperTradingSession) => {
      paperTradingSessions.update((s) => ({ ...s, [labRecordId]: session }));
      paperTradingLabRecordId.set(labRecordId);
    }),
  };
}

export type RunServiceStub = ReturnType<typeof createRunServiceStub>;
