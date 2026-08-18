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

  it('no longer registers the retired agent-console route', () => {
    const shell = routes[0];
    const children = (shell?.children ?? []) as Route[];
    expect(children.some((r) => r.path === 'agent-console')).toBe(false);
  });

  it('redirects legacy agent-provisioning to /agent-studio/provisioning', () => {
    const shell = routes[0];
    const children = (shell?.children ?? []) as { path?: string; redirectTo?: string }[];
    const redirect = children.find((r) => r.path === 'agent-provisioning');
    expect(redirect?.redirectTo).toBe('/agent-studio/provisioning');
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
      // Every child is either a redirect or a lazily-loaded component — none
      // should pin an eager `component` (which would pull it into the main bundle).
      if (route.redirectTo !== undefined) {
        continue;
      }
      expect(route.component).toBeUndefined();
      expect(typeof route.loadComponent).toBe('function');
      // Invoke the loader so a bad import path / class name fails here, not at runtime.
      const cmp = await route.loadComponent!();
      expect(typeof cmp).toBe('function'); // a component class
    }
  });

  it('nests persona-run under agent-studio and keeps the old audit route', async () => {
    const shell = routes[0];
    const children = (shell?.children ?? []) as Route[];
    const studio = children.find((r) => r.path === 'agent-studio');
    expect(studio).toBeDefined();
    expect(studio!.children?.length).toBe(2);
    expect(studio!.children?.map((c) => c.path)).toEqual(
      expect.arrayContaining(['', 'persona-run/:runId']),
    );
    const auditChild = studio!.children?.find((c) => c.path === 'persona-run/:runId');
    expect(auditChild?.data).toEqual({ hideStudioFooter: true });
    expect(typeof auditChild?.loadComponent).toBe('function');
    const { AgentStudioPersonaAuditComponent } = await import(
      './components/agent-team-studio/agent-studio-shell/agent-studio-persona-audit.component'
    );
    expect(await auditChild!.loadComponent!()).toBe(AgentStudioPersonaAuditComponent);

    const emptyChild = studio!.children?.find((c) => c.path === '');
    expect(typeof emptyChild?.loadComponent).toBe('function');
    const { AgentStudioStageHostComponent } = await import(
      './components/agent-team-studio/agent-studio-shell/agent-studio-stage-host.component'
    );
    expect(await emptyChild!.loadComponent!()).toBe(AgentStudioStageHostComponent);

    const oldAudit = children.find((r) => r.path === 'persona-testing/audit/:runId');
    expect(oldAudit).toBeDefined();
    expect(typeof oldAudit?.loadComponent).toBe('function');
  });
});
