import { TestBed } from '@angular/core/testing';
import { ActivatedRouteSnapshot, Router, UrlTree, convertToParamMap } from '@angular/router';
import { personaAuditRedirectGuard } from './persona-audit-redirect.guard';

describe('personaAuditRedirectGuard', () => {
  let router: Router;

  beforeEach(() => {
    TestBed.configureTestingModule({});
    router = TestBed.inject(Router);
  });

  function buildRouteSnapshot(runId: string): ActivatedRouteSnapshot {
    return { paramMap: convertToParamMap({ runId }) } as unknown as ActivatedRouteSnapshot;
  }

  it('returns a UrlTree redirecting to /agent-studio/persona-run/:runId', () => {
    const route = buildRouteSnapshot('abc-123');
    const result = TestBed.runInInjectionContext(() =>
      personaAuditRedirectGuard(route, {} as never),
    );
    expect(result).toBeInstanceOf(UrlTree);
    expect((result as UrlTree).toString()).toBe('/agent-studio/persona-run/abc-123');
  });

  it('preserves a different runId in the redirect', () => {
    const route = buildRouteSnapshot('run-xyz-456');
    const result = TestBed.runInInjectionContext(() =>
      personaAuditRedirectGuard(route, {} as never),
    );
    expect((result as UrlTree).toString()).toBe('/agent-studio/persona-run/run-xyz-456');
  });

  it('handles missing runId gracefully (empty string)', () => {
    const route = { paramMap: convertToParamMap({}) } as unknown as ActivatedRouteSnapshot;
    const result = TestBed.runInInjectionContext(() =>
      personaAuditRedirectGuard(route, {} as never),
    );
    expect((result as UrlTree).toString()).toBe('/agent-studio/persona-run/');
  });
});
