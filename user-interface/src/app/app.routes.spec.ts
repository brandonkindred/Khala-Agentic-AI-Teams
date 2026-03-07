import { routes } from './app.routes';
import { JobsDashboardComponent } from './components/jobs-dashboard/jobs-dashboard.component';
import { SoftwareEngineeringDashboardComponent } from './components/software-engineering-dashboard/software-engineering-dashboard.component';
import { IntegrationsDashboardComponent } from './components/integrations-dashboard/integrations-dashboard.component';

describe('App routes', () => {
  it('should define route for dashboard with JobsDashboardComponent', () => {
    const shell = routes[0];
    const children = shell?.children as { path: string; component: unknown }[];
    const dashboard = children?.find((r) => r.path === 'dashboard');
    expect(dashboard).toBeDefined();
    expect(dashboard?.component).toBe(JobsDashboardComponent);
  });

  it('should define route for software-engineering with SoftwareEngineeringDashboardComponent', () => {
    const shell = routes[0];
    const children = shell?.children as { path: string; component: unknown }[];
    const se = children?.find((r) => r.path === 'software-engineering');
    expect(se).toBeDefined();
    expect(se?.component).toBe(SoftwareEngineeringDashboardComponent);
  });

  it('should define route for integrations with IntegrationsDashboardComponent', () => {
    const shell = routes[0];
    const children = shell?.children as { path: string; component: unknown }[];
    const int = children?.find((r) => r.path === 'integrations');
    expect(int).toBeDefined();
    expect(int?.component).toBe(IntegrationsDashboardComponent);
  });

  it('should redirect empty path to /dashboard', () => {
    const shell = routes[0];
    const children = shell?.children as { path: string; redirectTo: string }[];
    const empty = children?.find((r) => r.path === '');
    expect(empty?.redirectTo).toBe('/dashboard');
  });
});
