import { Routes } from '@angular/router';
import { AppShellComponent } from './components/app-shell/app-shell.component';
import { llmConfiguredGuard } from './core/llm-configured.guard';

// All feature routes are lazily loaded so the initial bundle ships only the app
// shell; each dashboard (and its Angular Material deps) is fetched on first
// navigation to its route. Routes that share a component share one lazy chunk.
export const routes: Routes = [
  {
    path: '',
    component: AppShellComponent,
    // Prompt the operator to configure an LLM when the provider list is empty
    // (runs on every top-level navigation; self-skips /llm-config; never blocks).
    canActivateChild: [llmConfiguredGuard],
    children: [
      { path: '', redirectTo: '/dashboard', pathMatch: 'full' },
      {
        path: 'dashboard',
        loadComponent: () =>
          import('./components/jobs-dashboard/jobs-dashboard.component').then((m) => m.JobsDashboardComponent),
        data: { breadcrumb: 'Jobs Dashboard', title: 'Jobs Dashboard' },
      },
      {
        path: 'blogging',
        loadComponent: () =>
          import('./components/blog-landing/blog-landing.component').then((m) => m.BlogLandingComponent),
        data: { breadcrumb: 'Blogging', title: 'Blogging' },
      },
      {
        path: 'blogging/dashboard',
        loadComponent: () =>
          import('./components/blogging-dashboard/blogging-dashboard.component').then(
            (m) => m.BloggingDashboardComponent,
          ),
        data: { breadcrumb: 'Pipeline Dashboard', title: 'Blogging Pipeline' },
      },
      {
        path: 'blogging/jobs/:jobId/artifacts/:artifactName',
        loadComponent: () =>
          import('./components/blog-artifact-viewer/blog-artifact-viewer.component').then(
            (m) => m.BlogArtifactViewerComponent,
          ),
        data: { breadcrumb: 'Artifact', title: 'Artifact Viewer' },
      },
      {
        path: 'software-engineering',
        loadComponent: () =>
          import('./components/software-engineering-dashboard/software-engineering-dashboard.component').then(
            (m) => m.SoftwareEngineeringDashboardComponent,
          ),
        data: { breadcrumb: 'Software Engineering', title: 'Software Engineering' },
      },
      {
        path: 'software-engineering/planning-v3',
        loadComponent: () =>
          import('./components/planning-v3-page/planning-v3-page.component').then((m) => m.PlanningV3PageComponent),
        data: { breadcrumb: 'Planning', title: 'Planning' },
      },
      {
        path: 'software-engineering/coding-team',
        loadComponent: () =>
          import('./components/coding-team-page/coding-team-page.component').then((m) => m.CodingTeamPageComponent),
        data: { breadcrumb: 'Coding Team', title: 'Coding Team' },
      },
      {
        path: 'software-engineering/code-review',
        loadComponent: () =>
          import('./components/code-review-panel/code-review-panel.component').then((m) => m.CodeReviewPanelComponent),
        data: { breadcrumb: 'Code Review', title: 'Code Review' },
      },
      {
        path: 'software-engineering/planning-v2/jobs/:jobId/artifacts/:artifactName',
        loadComponent: () =>
          import('./components/planning-artifact-detail/planning-artifact-detail.component').then(
            (m) => m.PlanningArtifactDetailComponent,
          ),
        data: { breadcrumb: 'Artifact', title: 'Planning Artifact' },
      },
      {
        path: 'market-research',
        loadComponent: () =>
          import('./components/market-research-dashboard/market-research-dashboard.component').then(
            (m) => m.MarketResearchDashboardComponent,
          ),
        data: { breadcrumb: 'Market Research', title: 'Market Research' },
      },
      {
        path: 'soc2-compliance',
        loadComponent: () =>
          import('./components/soc2-compliance-dashboard/soc2-compliance-dashboard.component').then(
            (m) => m.Soc2ComplianceDashboardComponent,
          ),
        data: { breadcrumb: 'SOC2 Compliance', title: 'SOC2 Compliance' },
      },
      {
        path: 'social-marketing',
        loadComponent: () =>
          import('./components/social-marketing-dashboard/social-marketing-dashboard.component').then(
            (m) => m.SocialMarketingDashboardComponent,
          ),
        data: { breadcrumb: 'Social Marketing', title: 'Social Marketing' },
      },
      {
        path: 'branding',
        loadComponent: () =>
          import('./components/branding-dashboard/branding-dashboard.component').then((m) => m.BrandingDashboardComponent),
        data: { breadcrumb: 'Branding', title: 'Branding' },
      },
      {
        path: 'personal-assistant',
        loadComponent: () =>
          import('./components/personal-assistant-dashboard/personal-assistant-dashboard.component').then(
            (m) => m.PersonalAssistantDashboardComponent,
          ),
        data: { breadcrumb: 'Personal Assistant', title: 'Personal Assistant' },
      },
      {
        path: 'accessibility',
        loadComponent: () =>
          import('./components/accessibility-dashboard/accessibility-dashboard.component').then(
            (m) => m.AccessibilityDashboardComponent,
          ),
        data: { breadcrumb: 'Accessibility Audit', title: 'Accessibility Audit' },
      },
      {
        path: 'agent-studio',
        loadComponent: () =>
          import('./components/agent-studio-shell/agent-studio-shell.component').then(
            (m) => m.AgentStudioShellComponent,
          ),
        data: { breadcrumb: 'Agent Studio', title: 'Agent Studio' },
      },
      {
        path: 'agent-console',
        loadComponent: () =>
          import('./components/agent-console/agent-console.component').then((m) => m.AgentConsoleComponent),
        data: { breadcrumb: 'Agent Console', title: 'Agent Console' },
      },
      { path: 'agent-provisioning', redirectTo: '/agent-console', pathMatch: 'full' },
      {
        path: 'ai-systems',
        loadComponent: () =>
          import('./components/ai-systems-dashboard/ai-systems-dashboard.component').then(
            (m) => m.AISystemsDashboardComponent,
          ),
        data: { breadcrumb: 'AI Systems', title: 'AI Systems' },
      },
      {
        path: 'investment',
        loadComponent: () =>
          import('./components/investment-dashboard/investment-dashboard.component').then(
            (m) => m.InvestmentDashboardComponent,
          ),
        data: { breadcrumb: 'Investment', title: 'Investment' },
      },
      {
        path: 'investment/advisor',
        loadComponent: () =>
          import('./components/investment-dashboard/investment-dashboard.component').then(
            (m) => m.InvestmentDashboardComponent,
          ),
        data: { investmentFocus: 'advisor', breadcrumb: 'Advisor & IPS', title: 'Investment Advisor' },
      },
      {
        path: 'investment/strategy-lab',
        loadComponent: () =>
          import('./components/investment-strategy-lab-page/investment-strategy-lab-page.component').then(
            (m) => m.InvestmentStrategyLabPageComponent,
          ),
        data: { breadcrumb: 'Strategy Lab', title: 'Strategy Lab' },
      },
      {
        path: 'integrations',
        loadComponent: () =>
          import('./components/integrations-dashboard/integrations-dashboard.component').then(
            (m) => m.IntegrationsDashboardComponent,
          ),
        data: { breadcrumb: 'Integrations', title: 'Integrations' },
      },
      {
        path: 'user-profile',
        loadComponent: () =>
          import('./components/user-profile/user-profile.component').then((m) => m.UserProfileComponent),
        data: { breadcrumb: 'User Profile', title: 'User Profile' },
      },
      {
        path: 'llm-config',
        loadComponent: () =>
          import('./components/llm-config-dashboard/llm-config-dashboard.component').then(
            (m) => m.LlmConfigDashboardComponent,
          ),
        data: { breadcrumb: 'LLM Provider', title: 'LLM Provider' },
      },
      {
        path: 'sales',
        loadComponent: () =>
          import('./components/sales-dashboard/sales-dashboard.component').then((m) => m.SalesDashboardComponent),
        data: { breadcrumb: 'Sales', title: 'Sales' },
      },
      {
        path: 'nutrition',
        loadComponent: () =>
          import('./components/nutrition-dashboard/nutrition-dashboard.component').then((m) => m.NutritionDashboardComponent),
        data: { breadcrumb: 'Nutritionist', title: 'Nutritionist' },
      },
      {
        path: 'agentic-teams',
        loadComponent: () =>
          import('./components/agentic-team-dashboard/agentic-team-dashboard.component').then(
            (m) => m.AgenticTeamDashboardComponent,
          ),
        data: { breadcrumb: 'Agentic Teams', title: 'Agentic Teams' },
      },
      {
        path: 'startup-advisor',
        loadComponent: () =>
          import('./components/startup-advisor-dashboard/startup-advisor-dashboard.component').then(
            (m) => m.StartupAdvisorDashboardComponent,
          ),
        data: { breadcrumb: 'Startup Advisor', title: 'Startup Advisor' },
      },
      {
        path: 'persona-testing',
        loadComponent: () =>
          import('./components/persona-testing-dashboard/persona-testing-dashboard.component').then(
            (m) => m.PersonaTestingDashboardComponent,
          ),
        data: { breadcrumb: 'Testing Personas', title: 'Testing Personas' },
      },
      {
        path: 'persona-testing/audit/:runId',
        loadComponent: () =>
          import('./components/persona-test-audit-panel/persona-test-audit-panel.component').then(
            (m) => m.PersonaTestAuditPanelComponent,
          ),
        data: { breadcrumb: 'Audit', title: 'Testing Personas Audit' },
      },
      {
        path: 'deepthought',
        loadComponent: () =>
          import('./components/deepthought-dashboard/deepthought-dashboard.component').then(
            (m) => m.DeepthoughtDashboardComponent,
          ),
        data: { breadcrumb: 'Deepthought', title: 'Deepthought' },
      },
      {
        path: 'road-trip-planning',
        loadComponent: () =>
          import('./components/road-trip-planning-dashboard/road-trip-planning-dashboard.component').then(
            (m) => m.RoadTripPlanningDashboardComponent,
          ),
        data: { breadcrumb: 'Road Trip Planning', title: 'Road Trip Planning' },
      },
    ],
  },
  { path: '**', redirectTo: '/dashboard' },
];
