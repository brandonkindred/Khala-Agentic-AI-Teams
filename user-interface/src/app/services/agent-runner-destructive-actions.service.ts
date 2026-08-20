import { DestroyRef, Injectable, inject, signal } from '@angular/core';
import { Observable, Subject } from 'rxjs';

import { AgentRunnerApiService } from './agent-runner-api.service';
import { NotificationService } from '../core/notification.service';
import { ConfirmDestructiveService } from '../shared/confirm-destructive.service';
import { DestructiveActionHelper } from '../shared/destructive-action.helper';

/**
 * Owns destructive-action concerns (confirm dialog, re-entrancy guard, API
 * call, loading state, error surfacing) for the Agent Runner tab.
 *
 * Uses the shared `DestructiveActionHelper` for the confirm → execute flow,
 * keeping this service as a thin feature-specific wrapper that supplies only
 * dialog data, the API call, and post-success side effects.
 *
 * Not `providedIn: 'root'` — intended to be provided at the consuming
 * component so its lifecycle is scoped to that component.
 */
@Injectable()
export class AgentRunnerDestructiveActionsService {
  private readonly api = inject(AgentRunnerApiService);
  private readonly destroyRef = inject(DestroyRef);

  /** Error-banner messages for this service's concerns; `null` clears. */
  private readonly _errors = new Subject<string | null>();
  readonly errors$: Observable<string | null> = this._errors.asObservable();

  private readonly helper = new DestructiveActionHelper(
    inject(ConfirmDestructiveService),
    inject(NotificationService),
    this.destroyRef,
    (msg) => this._errors.next(msg),
  );

  /** Saved-input id currently being deleted (disables actions on that row). */
  readonly deletingSavedInputId = signal<string | null>(null);
  /** True while a sandbox teardown is in flight. */
  readonly tearingDown = signal(false);

  /** Emits the deleted saved-input id after a successful deletion. */
  private readonly _savedInputDeleted = new Subject<string>();
  readonly savedInputDeleted$: Observable<string> = this._savedInputDeleted.asObservable();

  /** Emits after a successful sandbox teardown so the component can update state. */
  private readonly _sandboxTornDown = new Subject<void>();
  readonly sandboxTornDown$: Observable<void> = this._sandboxTornDown.asObservable();

  /**
   * Deletes a saved input after the user confirms a danger dialog.
   *
   * Preconditions: `savedId` and `savedName` are non-empty strings.
   * Side effects: opens `ConfirmDialogComponent`, calls
   *   `AgentRunnerApiService.deleteSavedInput`, shows a success toast,
   *   and emits `savedId` through `savedInputDeleted$`.
   * Postconditions: on completion (success or failure) `deletingSavedInputId`
   *   is reset to `null`. On failure, an error message is emitted through
   *   `errors$`.
   */
  deleteSavedInput(savedId: string, savedName: string): void {
    this.helper.execute(
      {
        dialogData: {
          title: 'Delete saved input',
          message: `Delete saved input "${savedName}"?\n\nThis cannot be undone.`,
          confirmLabel: 'Delete',
          variant: 'danger',
        },
        apiCall: () => this.api.deleteSavedInput(savedId),
        onSuccess: () => this._savedInputDeleted.next(savedId),
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
   *   and emits through `sandboxTornDown$`.
   * Postconditions: on completion (success or failure) `tearingDown`
   *   is reset to `false`. On failure, an error message is emitted
   *   through `errors$`.
   */
  tearDownSandbox(agentId: string, agentLabel: string): void {
    this.helper.execute(
      {
        dialogData: {
          title: 'Tear down sandbox',
          message: `Tear down the ${agentLabel} sandbox?\n\nThe sandbox will need to be re-warmed before the next invocation.`,
          confirmLabel: 'Tear down',
          variant: 'danger',
        },
        apiCall: () => this.api.teardown(agentId),
        onSuccess: () => this._sandboxTornDown.next(),
        errorFallback: 'Failed to tear down sandbox.',
      },
      'Sandbox torn down.',
      () => this.tearingDown.set(true),
      () => this.tearingDown.set(false),
    );
  }
}
