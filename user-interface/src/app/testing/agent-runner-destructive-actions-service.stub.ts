import { signal } from '@angular/core';
import { Subject } from 'rxjs';
import { vi } from 'vitest';
import type { AgentTaggedError, AgentTaggedEvent } from '../services/agent-runner-destructive-actions.service';

/**
 * An `AgentRunnerDestructiveActionsService` test double: real
 * `deletingSavedInputId`/`tearingDown` signals (so components read them
 * exactly as they would the live service) plus `Subject`s backing
 * `errors$`/`savedInputDeleted$`/`sandboxTornDown$`, with `vi.fn()` action
 * methods a test can assert against, mirroring
 * `createDestructiveActionsServiceStub()`'s shape in
 * `strategy-lab-destructive-actions-service.stub.ts`.
 *
 * Preconditions: none — call once per test (or per fixture) needing an
 *   isolated `AgentRunnerDestructiveActionsService` stand-in; each call
 *   returns fresh signals/Subjects/`vi.fn()`s, never shared state across
 *   calls.
 * Postconditions: returns an object whose `deletingSavedInputId` signal
 *   starts `null` and whose `tearingDown` signal starts `false` (matching
 *   the real service's field initializers), and whose `errors$`/
 *   `savedInputDeleted$`/`sandboxTornDown$` are fresh `Subject`s a test can
 *   `.next()` into to simulate the real service's emissions.
 */
export function createAgentRunnerDestructiveActionsServiceStub() {
  const deletingSavedInputId = signal<string | null>(null);
  const tearingDown = signal(false);
  const errors$ = new Subject<AgentTaggedError>();
  const savedInputDeleted$ = new Subject<AgentTaggedEvent<string>>();
  const sandboxTornDown$ = new Subject<AgentTaggedEvent>();
  return {
    deletingSavedInputId,
    tearingDown,
    errors$,
    savedInputDeleted$,
    sandboxTornDown$,
    deleteSavedInput: vi.fn(),
    tearDownSandbox: vi.fn(),
  };
}

export type AgentRunnerDestructiveActionsServiceStub = ReturnType<
  typeof createAgentRunnerDestructiveActionsServiceStub
>;
