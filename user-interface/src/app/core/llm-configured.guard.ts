import { Injectable, inject } from '@angular/core';
import { CanActivateChildFn, Router, RouterStateSnapshot } from '@angular/router';
import { MatDialog } from '@angular/material/dialog';
import { Observable, of } from 'rxjs';
import { catchError, map } from 'rxjs/operators';
import { LlmConfigApiService } from '../services/llm-config-api.service';
import {
  ConfirmDialogComponent,
  ConfirmDialogData,
} from '../shared/confirm-dialog/confirm-dialog.component';

/**
 * Session state for the "no LLMs configured" prompt so the guard neither re-queries
 * the API once it has seen a configured list, nor re-opens the dialog on every
 * navigation. Provided in root so TestBed gives each test a fresh instance.
 */
@Injectable({ providedIn: 'root' })
export class LlmSetupState {
  /** True once the provider list has been observed non-empty (stop checking). */
  configured = false;
  /** True once the "no LLMs" dialog has been shown this session (show it once). */
  prompted = false;
}

/**
 * Guard that prompts the operator to configure an LLM when none is set up.
 *
 * The Postgres-backed provider list is the sole source of LLM resolution, so with an
 * empty list every agent run fails. This `canActivateChild` guard runs on each
 * top-level navigation and, when the list is empty, floats a "No LLMs configured"
 * dialog whose "Setup LLM" button routes to `/llm-config`. It never blocks
 * navigation (the app stays usable) and:
 * - self-skips `/llm-config` so the setup page is always reachable;
 * - shows the dialog at most once per session and stops querying once configured;
 * - fails open (allows navigation, no dialog) on any API/probe error, so a transient
 *   Postgres blip never spuriously prompts or blocks.
 */
export const llmConfiguredGuard: CanActivateChildFn = (
  _route,
  state: RouterStateSnapshot,
): Observable<boolean> | boolean => {
  const setup = inject(LlmSetupState);
  // Already configured, already prompted, or heading to the setup page → allow.
  if (setup.configured || setup.prompted || state.url.startsWith('/llm-config')) {
    return true;
  }

  const api = inject(LlmConfigApiService);
  const dialog = inject(MatDialog);
  const router = inject(Router);

  return api.listProviders().pipe(
    map((res) => {
      if ((res.providers ?? []).length > 0) {
        setup.configured = true;
        return true;
      }
      setup.prompted = true;
      const data: ConfirmDialogData = {
        title: 'No LLMs configured',
        message:
          'No LLM providers are set up, so agents have no model to run. Add a provider ' +
          'to get started.',
        confirmLabel: 'Setup LLM',
        cancelLabel: 'Dismiss',
        variant: 'default',
      };
      dialog
        .open<ConfirmDialogComponent, ConfirmDialogData, boolean>(ConfirmDialogComponent, { data })
        .afterClosed()
        .subscribe((confirmed) => {
          if (confirmed) {
            router.navigateByUrl('/llm-config');
          }
        });
      return true; // don't block — the dialog floats on top of the target page
    }),
    // A probe/network error is not "no LLMs configured": fail open, no prompt.
    catchError(() => of(true)),
  );
};
