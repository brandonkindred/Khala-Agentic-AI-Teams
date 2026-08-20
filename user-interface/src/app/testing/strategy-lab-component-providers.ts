import type { Provider } from '@angular/core';
import { StrategyLabRunService } from '../services/strategy-lab-run.service';
import { StrategyLabActivityLogService } from '../services/strategy-lab-activity-log.service';
import { StrategyLabPaperTradingService } from '../services/strategy-lab-paper-trading.service';
import { StrategyLabDestructiveActionsService } from '../services/strategy-lab-destructive-actions.service';
import { ConfirmDestructiveService } from '../shared/confirm-destructive.service';
import type { RunServiceStub } from './strategy-lab-run-service.stub';

/**
 * The `overrideComponent(StrategyLabComponent, ...)` override every
 * `StrategyLabComponent` fixture needs: a caller-supplied `StrategyLabRunService`
 * stand-in (a fresh `createRunServiceStub()`, or a captured variable a test
 * mutates later), plus the real `StrategyLabActivityLogService`/
 * `StrategyLabPaperTradingService`/`StrategyLabDestructiveActionsService` —
 * one shared definition instead of a copy-pasted providers array per
 * describe block/spec file.
 *
 * Preconditions: none.
 * Postconditions: returns an `overrideComponent` metadata override whose
 *   `providers` array provides `StrategyLabRunService` as `runService`, then
 *   the real `StrategyLabActivityLogService`/`StrategyLabPaperTradingService`/
 *   `StrategyLabDestructiveActionsService`, then `extraProviders` in order —
 *   so an entry in `extraProviders` for a token already listed (e.g.
 *   overriding `StrategyLabPaperTradingService` with a stub) wins, per
 *   Angular's last-provider-for-a-token-wins rule.
 */
export function strategyLabProvidersOverride(runService: RunServiceStub, extraProviders: Provider[] = []) {
  return {
    set: {
      providers: [
        { provide: StrategyLabRunService, useValue: runService },
        StrategyLabActivityLogService,
        StrategyLabPaperTradingService,
        StrategyLabDestructiveActionsService,
        ConfirmDestructiveService,
        ...extraProviders,
      ],
    },
  };
}
