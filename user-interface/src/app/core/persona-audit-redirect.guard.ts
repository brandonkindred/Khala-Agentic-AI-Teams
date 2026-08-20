import { inject } from '@angular/core';
import { ActivatedRouteSnapshot, CanActivateFn, Router } from '@angular/router';

/**
 * Redirect guard for the retired `/persona-testing/audit/:runId` deep links.
 *
 * Preconditions: none — the route may provide a `:runId` param; a missing
 *   value is treated as an empty string.
 * Postconditions: returns a `UrlTree` pointing to `/agent-studio/persona-run/:runId`,
 *   preserving the run ID so bookmarked audit links continue to work.
 *
 * This guard exists so that stale bookmarks and external references to the old
 * `/persona-testing/audit/:runId` path resolve to the relocated persona audit
 * surface under Agent Studio.
 */
export const personaAuditRedirectGuard: CanActivateFn = (route: ActivatedRouteSnapshot) => {
  const router = inject(Router);
  const runId = route.paramMap.get('runId') ?? '';
  return router.createUrlTree(['/agent-studio', 'persona-run', runId]);
};
