import { DestroyRef, Injectable, inject, signal } from '@angular/core';
import { Observable, Subject } from 'rxjs';

import { AgentConsoleApiService } from '../../../../services/agent-console-api.service';
import type { AgentTaggedError, AgentTaggedEvent } from '../../../../services/agent-runner-destructive-actions.service';
import { NotificationService } from '../../../../core/notification.service';
import { ConfirmDestructiveService } from '../../../../shared/confirm-destructive.service';
import { DestructiveActionHelper } from '../../../../shared/destructive-action.helper';
import type { RunSummary } from '../../../../models/agent-history.model';

/**
 * Owns run deletion for `AgentRunHistoryComponent`, including the shared
 * destructive-action confirmation dialog. Mirrors
 * `StrategyLabDestructiveActionsService`'s relationship to its host: the
 * component has no direct dialog/API access into this service's concerns —
 * a successful delete is surfaced as `runDeleted$` for the component to
 * subscribe to once, rather than this service reaching into the component's
 * `runs` signal directly.
 *
 * Uses the shared `DestructiveActionHelper` for the confirm -> execute flow.
 *
 * All emissions carry the originating `agentId` (from `RunSummary.agent_id`),
 * mirroring `AgentRunnerDestructiveActionsService`, so the host component can
 * discard a stale event from a prior agent's in-flight delete — the same
 * component instance is reused across `agentId` input changes.
 *
 * Not `providedIn: 'root'` — intended to be provided at the consuming
 * component so its lifecycle is scoped to that component.
 */
@Injectable()
export class AgentRunHistoryDestructiveActionsService {
  private readonly api = inject(AgentConsoleApiService);
  private readonly destroyRef = inject(DestroyRef);

  /** Error-banner messages tagged with originating agent; `null` message clears. */
  private readonly _errors = new Subject<AgentTaggedError>();
  readonly errors$: Observable<AgentTaggedError> = this._errors.asObservable();

  private readonly helper = new DestructiveActionHelper(
    inject(ConfirmDestructiveService),
    inject(NotificationService),
    this.destroyRef,
    // Default onError is never used — each call provides its own via opts.onError.
    () => undefined,
  );

  /**
   * Run ids currently being deleted (disables actions on those rows). A set
   * rather than a single id so that confirming two deletes back-to-back —
   * possible now that the Material confirm dialog is async, unlike the
   * blocking native `confirm()` this replaced — doesn't have the second
   * delete's completion clear the first delete's still-in-flight state.
   */
  private readonly _deletingRunIds = signal<ReadonlySet<string>>(new Set());
  readonly deletingRunIds = this._deletingRunIds.asReadonly();

  isDeleting(runId: string): boolean {
    return this._deletingRunIds().has(runId);
  }

  private addDeletingRunId(runId: string): void {
    this._deletingRunIds.update((ids) => new Set(ids).add(runId));
  }

  private removeDeletingRunId(runId: string): void {
    this._deletingRunIds.update((ids) => {
      const next = new Set(ids);
      next.delete(runId);
      return next;
    });
  }

  /** Emits the deleted run's id tagged with the originating agent. */
  private readonly _runDeleted = new Subject<AgentTaggedEvent<string>>();
  readonly runDeleted$: Observable<AgentTaggedEvent<string>> = this._runDeleted.asObservable();

  /**
   * Deletes a run after user confirmation.
   *
   * Preconditions: `run.id`, `run.agent_id`, and `run.trace_id` are non-empty.
   * Postconditions: on completion (success or failure) `run.id` is removed
   *   from `deletingRunIds`. On success, emits `{ agentId: run.agent_id,
   *   payload: run.id }` through `runDeleted$` and shows a success toast. On
   *   failure, emits `{ agentId: run.agent_id, message: <detail> }` through
   *   `errors$` and no toast is shown.
   */
  deleteRun(run: RunSummary): void {
    this.helper.execute(
      {
        dialogData: {
          title: 'Delete run',
          message: `Delete run ${run.trace_id.slice(0, 8)}? This can't be undone.`,
          confirmLabel: 'Delete',
          variant: 'danger',
        },
        apiCall: () => this.api.deleteRun(run.id),
        onSuccess: () => this._runDeleted.next({ agentId: run.agent_id, payload: run.id }),
        errorFallback: 'Failed to delete run.',
        onError: (msg) => this._errors.next({ agentId: run.agent_id, message: msg }),
      },
      'Run deleted.',
      () => this.addDeletingRunId(run.id),
      () => this.removeDeletingRunId(run.id),
    );
  }
}
