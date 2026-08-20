/**
 * Route & Nav Cutover Tests — Issue #6525
 *
 * Verifies that the UI cutover (3/4) correctly:
 *  1. Removes /agent-console, /agentic-teams, /persona-testing, and the old audit route.
 *  2. Exposes only Agent Studio as the product entry in navigation.
 *  3. Resolves the persona live audit at its Studio nested route.
 */
import { Route } from '@angular/router';
import { routes } from './app.routes';
import { NAV_GROUPS, ALL_NAV_ITEMS } from './models/navigation.model';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** All first-level child routes of the app shell. */
function shellChildren(): Route[] {
  return (routes[0]?.children ?? []) as Route[];
}

/** Whether a given path is registered as a shell child route. */
function hasRoute(path: string): boolean {
  return shellChildren().some((r) => r.path === path);
}

// ---------------------------------------------------------------------------
// AC 1: Deleted routes no longer resolve
// ---------------------------------------------------------------------------

describe('[Cutover] Deleted routes do not resolve', () => {
  it('/agent-console is not registered', () => {
    expect(hasRoute('agent-console')).toBe(false);
  });

  it('/agentic-teams is not registered', () => {
    expect(hasRoute('agentic-teams')).toBe(false);
  });

  it('/persona-testing is not registered', () => {
    expect(hasRoute('persona-testing')).toBe(false);
  });

  it('/persona-testing/audit/:runId (old audit) is not registered', () => {
    expect(hasRoute('persona-testing/audit/:runId')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// AC 2: Nav exposes only Agent Studio as the product entry
// ---------------------------------------------------------------------------

describe('[Cutover] Nav lists only Agent Studio as the product entry', () => {
  const agenticGroup = NAV_GROUPS.find((g) => g.key === 'agentic-ai')!;

  it('agentic-ai group exists', () => {
    expect(agenticGroup).toBeDefined();
  });

  it('Agent Studio is the first non-nested item in the agentic-ai group', () => {
    const topLevel = agenticGroup.items.filter((i) => !i.nested);
    expect(topLevel[0].id).toBe('agent-studio');
  });

  it('no retired products remain as nav items', () => {
    const retiredIds = ['agent-console', 'agentic-teams', 'persona-testing'];
    const retiredRoutes = ['/agent-console', '/agentic-teams', '/persona-testing'];

    for (const id of retiredIds) {
      expect(ALL_NAV_ITEMS.some((i) => i.id === id)).toBe(false);
    }
    for (const route of retiredRoutes) {
      expect(ALL_NAV_ITEMS.some((i) => i.route === route)).toBe(false);
    }
  });

  it('Agent Studio is marked exact so child routes do not co-activate its link', () => {
    const studio = ALL_NAV_ITEMS.find((i) => i.id === 'agent-studio');
    expect(studio).toBeDefined();
    expect(studio!.exact).toBe(true);
    expect(studio!.route).toBe('/agent-studio');
  });

  it('Provisioning and Metrics are nested under Agent Studio in nav', () => {
    const provisioning = ALL_NAV_ITEMS.find((i) => i.id === 'agent-studio-provisioning');
    const metrics = ALL_NAV_ITEMS.find((i) => i.id === 'agent-studio-metrics');

    expect(provisioning).toBeDefined();
    expect(provisioning!.nested).toBe(true);
    expect(provisioning!.route).toBe('/agent-studio/provisioning');

    expect(metrics).toBeDefined();
    expect(metrics!.nested).toBe(true);
    expect(metrics!.route).toBe('/agent-studio/metrics');
  });
});

// ---------------------------------------------------------------------------
// AC 3: Persona live audit resolves at its Studio nested route
// ---------------------------------------------------------------------------

describe('[Cutover] Persona live audit resolves at agent-studio nested route', () => {
  const studioRoute = shellChildren().find((r) => r.path === 'agent-studio');

  it('agent-studio route exists with child routes', () => {
    expect(studioRoute).toBeDefined();
    expect(studioRoute!.children?.length).toBeGreaterThanOrEqual(2);
  });

  it('persona-run/:runId is a child of agent-studio', () => {
    const auditChild = studioRoute!.children?.find((c) => c.path === 'persona-run/:runId');
    expect(auditChild).toBeDefined();
  });

  it('persona-run/:runId hides the studio footer', () => {
    const auditChild = studioRoute!.children?.find((c) => c.path === 'persona-run/:runId');
    expect(auditChild?.data).toEqual({ hideStudioFooter: true });
  });

  it('persona-run/:runId lazily loads AgentStudioPersonaAuditComponent', async () => {
    const auditChild = studioRoute!.children?.find((c) => c.path === 'persona-run/:runId');
    expect(typeof auditChild?.loadComponent).toBe('function');

    const { AgentStudioPersonaAuditComponent } = await import(
      './components/agent-team-studio/agent-studio-shell/agent-studio-persona-audit.component'
    );
    expect(await auditChild!.loadComponent!()).toBe(AgentStudioPersonaAuditComponent);
  });

  it('default child renders AgentStudioStageHostComponent (not a redirect)', async () => {
    const emptyChild = studioRoute!.children?.find((c) => c.path === '');
    expect(typeof emptyChild?.loadComponent).toBe('function');

    const { AgentStudioStageHostComponent } = await import(
      './components/agent-team-studio/agent-studio-shell/agent-studio-stage-host.component'
    );
    expect(await emptyChild!.loadComponent!()).toBe(AgentStudioStageHostComponent);
  });
});
