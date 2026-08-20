import { DestroyRef, Injectable, inject, signal } from '@angular/core';
import { Observable, Subject } from 'rxjs';

import { AgentRunnerApiService } from './agent-runner-api.service';
import { NotificationService } from '../core/notification.service';
import { ConfirmDestructiveService } from '../shared/confirm-destructive.service';
import { DestructiveActionHelper } from '../shared/destructive-action.helper';

/** Emission from a destructive action tagged with the originating agent. */
export interface AgentTaggedEvent<T = void> {
  agentId: string;
  payload: T;
}

/** Error emission tagged with the originating agent; `null` message clears. */
export interface AgentTaggedError {
  agentId: string;
  message: string | null;
}

/**
 * Owns destructive-action concerns (confirm dialog, re-entrancy guard, API
 * call, loading state, error surfacing) for the Agent Runner tab.
 *
 * Uses the shared `DestructiveActionHelper` for the confirm → execute flow,
 * keeping this service as a thin feature-specific wrapper that supplies only
 * dialog data, the API call, and post-success side effects.
 *
 * All emissions carry the originating `agentId` so the host component can
 * discard stale events from a prior agent's in-flight action.
 *
 * Not `providedIn: 'root'` — intended to be provided at the consuming
 * component so its lifecycle is scoped to that component.
 */
@Injectable()
export class AgentRunnerDestructiveActionsService {
  private readonly api = inject(AgentRunnerApiService);
  private readonly destroyRef = inject(DestroyRef);

  /** Error-banner messages tagged with originating agent; `null` message clears. */
  private readonly _errors = new Subject<AgentTaggedError>();
  readonly errors$: Observable<AgentTaggedError> = this._errors.asObservable();

  private readonly helper = new DestructiveActionHelper(
    inject(ConfirmDestructiveService),
    inject(NotificationService),
    this.destroyRef,
    (msg) => this._emitError(msg),
  );

  /** Agent id of the currently executing action (set by public methods). */
  private _currentAgentId: string | null = null;

  /** Saved-input id currently being deleted (disables actions on that row). */
  readonly deletingSavedInputId = signal<string | null>(null);
  /** True while a sandbox teardown is in flight. */
  readonly tearingDown = signal(false);

  /** Emits the deleted saved-input id tagged with the originating agent. */
  private readonly _savedInputDeleted = new Subject<AgentTaggedEvent<string>>();
  readonly savedInputDeleted$: Observable<AgentTaggedEvent<string>> = this._savedInputDeleted.asObservable();

  /** Emits after a successful sandbox teardown tagged with the originating agent. */
  private readonly _sandboxTornDown = new Subject<AgentTaggedEvent>();
  readonly sandboxTornDown$: Observable<AgentTaggedEvent> = this._sandboxTornDown.asObservable();

  private _emitError(msg: string | null): void {
    if (this._currentAgentId) {
      this._errors.next({ agentId: this._currentAgentId, message: msg });
    }
  }

  /**
   * Deletes a saved input after the user confirms a danger dialog.
   *
   * Preconditions: `agentId`, `savedId`, and `savedName` are non-empty strings.
   * Side effects: opens `ConfirmDialogComponent`, calls
   *   `AgentRunnerApiService.deleteSavedInput`, shows a success toast,
   *   and emits through `savedInputDeleted$` tagged with the agent id.
   * Postconditions: on completion (success or failure) `deletingSavedInputId`
   *   is reset to `null`. On failure, an error is emitted through `errors$`.
   */
  deleteSavedInput(agentId: string, savedId: string, savedName: string): void {
    this._currentAgentId = agentId;
    this.helper.execute(
      {
        dialogData: {
          title: 'Delete saved input',
          message: `Delete saved input "${savedName}"?\n\nThis cannot be undone.`,
          confirmLabel: 'Delete',
          variant: 'danger',
        },
        apiCall: () => this.api.deleteSavedInput(savedId),
        onSuccess: () => this._savedInputDeleted.next({ agentId, payload: savedId }),
        errorFallback: 'Failed to delete saved input.',
      },
      'Saved input deleted.',
      () => this.deletingSavedInputId.set(savedId),
      () => this.deletingSavedInputId.set(null),
    );
  }

  /**
   * Tears down a sandbox after the user confirms a danger dialog.
   *
   * Preconditions: `agentId` is a non-empty agent identifier;
   *   `agentLabel` is the human-readable name shown in the dialog.
   * Side effects: opens `ConfirmDialogComponent`, calls
   *   `AgentRunnerApiService.teardown`, shows a success toast,
   *   and emits through `sandboxTornDown$` tagged with the agent id.
   * Postconditions: on completion (success or failure) `tearingDown`
   *   is reset to `false`. On failure, an error is emitted through `errors$`.
   */
  tearDownSandbox(agentId: string, agentLabel: string): void {
    this._currentAgentId = agentId;
    this.helper.execute(
      {
        dialogData: {
          title: 'Tear down sandbox',
          message: `Tear down the ${agentLabel} sandbox?\n\nThe sandbox will need to be re-warmed before the next invocation.`,
          confirmLabel: 'Tear down',
          variant: 'danger',
        },
        apiCall: () => this.api.teardown(agentId),
        onSuccess: () => this._sandboxTornDown.next({ agentId, payload: undefined }),
        errorFallback: 'Failed to tear down sandbox.',
      },
      'Sandbox torn down.',
      () => this.tearingDown.set(true),
      () => this.tearingDown.set(false),
    );
  }
}
