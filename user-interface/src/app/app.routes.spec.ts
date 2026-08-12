import { Route } from '@angular/router';
import { routes } from './app.routes';
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
});
