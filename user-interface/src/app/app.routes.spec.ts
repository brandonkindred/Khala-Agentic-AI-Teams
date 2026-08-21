import { Route } from '@angular/router';
import { routes } from './app.routes';
import { unsavedChangesGuard } from './core/unsaved-changes.guard';
import { JobsDashboardComponent } from './components/jobs-dashboard/jobs-dashboard.component';
import { SoftwareEngineeringDashboardComponent } from './components/software-engineering-dashboard/software-engineering-dashboard.component';
import { IntegrationsDashboardComponent } from './components/integrations-dashboard/integrations-dashboard.component';
import { LlmConfigDashboardComponent } from './components/llm-config-dashboard/llm-config-dashboard.component';

/** Resolve a child route's lazily-loaded component to its class. */
async function loadedComponent(path: string): Promise<unknown> {
  const shell = routes[0];
  const children = (shell?.children ?? []) as Route[];
  const route = children.find((r) => r.path === path);
  expect(route).toBeDefined();
  expect(typeof route?.loadComponent).toBe('function');
  return route!.loadComponent!();
}

describe('App routes', () => {
  it('lazily loads JobsDashboardComponent for dashboard', async () => {
    expect(await loadedComponent('dashboard')).toBe(JobsDashboardComponent);
  });

  it('lazily loads SoftwareEngineeringDashboardComponent for software-engineering', async () => {
    expect(await loadedComponent('software-engineering')).toBe(SoftwareEngineeringDashboardComponent);
  });

  it('lazily loads IntegrationsDashboardComponent for integrations', async () => {
    expect(await loadedComponent('integrations')).toBe(IntegrationsDashboardComponent);
  });

  it('lazily loads LlmConfigDashboardComponent for llm-config', async () => {
    expect(await loadedComponent('llm-config')).toBe(LlmConfigDashboardComponent);
  });

  it('lazily loads LlmUsageDashboardComponent for llm-usage', async () => {
    const { LlmUsageDashboardComponent } = await import(
      './components/llm-usage-dashboard/llm-usage-dashboard.component'
    );
    expect(await loadedComponent('llm-usage')).toBe(LlmUsageDashboardComponent);
  });

  it('lazily loads JobMatchingDashboardComponent for job-matching', async () => {
    const { JobMatchingDashboardComponent } = await import(
      './components/job-matching-dashboard/job-matching-dashboard.component'
    );
    expect(await loadedComponent('job-matching')).toBe(JobMatchingDashboardComponent);
  });

  it('lazily loads ProductDeliveryPageComponent for product-delivery', async () => {
    const { ProductDeliveryPageComponent } = await import(
      './components/product-delivery-page/product-delivery-page.component'
    );
    expect(await loadedComponent('product-delivery')).toBe(ProductDeliveryPageComponent);
  });

  it('lazily loads CognitionPageComponent for cognition', async () => {
    const { CognitionPageComponent } = await import(
      './components/cognition-page/cognition-page.component'
    );
    expect(await loadedComponent('cognition')).toBe(CognitionPageComponent);
  });

  it('lazily loads AgentProvisioningDashboardComponent for agent-studio/provisioning', async () => {
    const { AgentProvisioningDashboardComponent } = await import(
      './components/agent-team-studio/agent-provisioning-dashboard/agent-provisioning-dashboard.component'
    );
    expect(await loadedComponent('agent-studio/provisioning')).toBe(AgentProvisioningDashboardComponent);
  });

  it('lazily loads MetricsTabComponent for agent-studio/metrics', async () => {
    const { MetricsTabComponent } = await import(
      './components/agent-team-studio/metrics-tab/metrics-tab.component'
    );
    expect(await loadedComponent('agent-studio/metrics')).toBe(MetricsTabComponent);
  });

  it('registers unsavedChangesGuard on the agent-studio route\'s canDeactivate', () => {
    const shell = routes[0];
    const children = (shell?.children ?? []) as Route[];
    const route = children.find((r) => r.path === 'agent-studio');
    expect(route?.canDeactivate).toContain(unsavedChangesGuard);
  });

  it('uses agent-studio as the sole agentic journey entry point (no legacy peer routes)', () => {
    const shell = routes[0];
    const children = (shell?.children ?? []) as Route[];
    // Studio route exists as entry point
    expect(children.some((r) => r.path === 'agent-studio')).toBe(true);
    // Legacy Console/Teams/Personas peer routes are absent
    const legacyPaths = ['agent-console', 'agentic-teams', 'agent-provisioning', 'persona-testing', 'persona-testing/audit/:runId'];
    for (const legacy of legacyPaths) {
      expect(children.some((r) => r.path === legacy)).toBe(false);
    }
  });

  it('redirects empty path to /dashboard', () => {
    const shell = routes[0];
    const children = shell?.children as { path: string; redirectTo: string }[];
    const empty = children?.find((r) => r.path === '');
    expect(empty?.redirectTo).toBe('/dashboard');
  });

  it('lazily loads every feature route (no eager bindings; each import resolves)', async () => {
    const shell = routes[0];
    const children = (shell?.children ?? []) as Route[];
    for (const route of children) {
      // Every child is either a redirect, a guard-only redirect (canActivate + children),
      // or a lazily-loaded component — none should pin an eager `component` (which would
      // pull it into the main bundle).
      if (route.redirectTo !== undefined) {
        continue;
      }
      // Guard-based redirect routes have canActivate + children:[] (no component).
      if (route.canActivate && route.children) {
        continue;
      }
      expect(route.component).toBeUndefined();
      expect(typeof route.loadComponent).toBe('function');
      // Invoke the loader so a bad import path / class name fails here, not at runtime.
      const cmp = await route.loadComponent!();
      expect(typeof cmp).toBe('function'); // a component class
    }
  });

  it('nests persona-run under agent-studio', async () => {
    const shell = routes[0];
    const children = (shell?.children ?? []) as Route[];
    const studio = children.find((r) => r.path === 'agent-studio');
    expect(studio).toBeDefined();
    expect(studio!.children?.length).toBe(2);
    expect(studio!.children?.map((c) => c.path)).toEqual(
      expect.arrayContaining(['', 'persona-run/:runId']),
    );
    const auditChild = studio!.children?.find((c) => c.path === 'persona-run/:runId');
    // Use toMatchObject so future data properties don't cause false negatives.
    expect(auditChild?.data).toMatchObject({ hideStudioFooter: true });
    expect(typeof auditChild?.loadComponent).toBe('function');
    // Verify lazy loaders resolve to a component class without coupling to concrete
    // file paths — the 'lazily loads every feature route' test already validates that
    // all loadComponent loaders resolve, so here we only assert structural correctness.
    const auditCmp = await auditChild!.loadComponent!();
    expect(typeof auditCmp).toBe('function');

    const emptyChild = studio!.children?.find((c) => c.path === '');
    expect(typeof emptyChild?.loadComponent).toBe('function');
    const stageCmp = await emptyChild!.loadComponent!();
    expect(typeof stageCmp).toBe('function');
  });

  it('routes all persona workflow traffic through agent-studio children', async () => {
    const shell = routes[0];
    const children = (shell?.children ?? []) as Route[];
    const studio = children.find((r) => r.path === 'agent-studio');
    expect(studio).toBeDefined();
    // Persona audit is nested under Studio — not a standalone route
    expect(studio!.children?.some((c) => c.path === 'persona-run/:runId')).toBe(true);
    // Studio sub-routes (provisioning, metrics) are siblings at top level.
    // NOTE: docs/design/agent-studio-ux-spec.md §2.3 plans to fold provisioning
    // into the Studio shell; when that lands (#5948), update this assertion.
    expect(children.some((r) => r.path === 'agent-studio/provisioning')).toBe(true);
    expect(children.some((r) => r.path === 'agent-studio/metrics')).toBe(true);
  });

  it('wildcard route redirects unmatched paths (including legacy bookmarks) to /dashboard', () => {
    const wildcard = routes.find((r) => r.path === '**');
    expect(wildcard).toBeDefined();
    expect(wildcard!.redirectTo).toBe('/dashboard');
  });

  it('agent-studio route carries correct title and breadcrumb metadata', () => {
    const shell = routes[0];
    const children = (shell?.children ?? []) as Route[];
    const studio = children.find((r) => r.path === 'agent-studio');
    expect(studio?.title).toBe('Agent Studio');
    expect(studio?.data).toMatchObject({ breadcrumb: 'Agent Studio' });
  });

  it('agent-studio/provisioning deep-link destination has correct metadata', () => {
    const shell = routes[0];
    const children = (shell?.children ?? []) as Route[];
    const provisioning = children.find((r) => r.path === 'agent-studio/provisioning');
    expect(provisioning?.title).toBe('Provisioning & Environments');
    expect(provisioning?.data).toMatchObject({ breadcrumb: 'Provisioning' });
  });

  it('agent-studio/metrics deep-link destination has correct metadata', () => {
    const shell = routes[0];
    const children = (shell?.children ?? []) as Route[];
    const metrics = children.find((r) => r.path === 'agent-studio/metrics');
    expect(metrics?.title).toBe('Metrics');
    expect(metrics?.data).toMatchObject({ breadcrumb: 'Metrics' });
  });
});
