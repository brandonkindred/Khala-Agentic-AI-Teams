import { Routes } from '@angular/router';
import { AppShellComponent } from './components/app-shell/app-shell.component';
import { llmConfiguredGuard } from './core/llm-configured.guard';
import { unsavedChangesGuard } from './core/unsaved-changes.guard';

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
        title: 'Jobs Dashboard',
        data: { breadcrumb: 'Jobs Dashboard' },
      },
      {
        path: 'blogging',
        loadComponent: () =>
          import('./components/blog-landing/blog-landing.component').then((m) => m.BlogLandingComponent),
        title: 'Blogging',
        data: { breadcrumb: 'Blogging' },
      },
      {
        path: 'blogging/dashboard',
        loadComponent: () =>
          import('./components/blogging-dashboard/blogging-dashboard.component').then(
            (m) => m.BloggingDashboardComponent,
          ),
        title: 'Blogging Pipeline',
        data: { breadcrumb: 'Pipeline Dashboard' },
      },
      {
        path: 'blogging/jobs/:jobId/artifacts/:artifactName',
        loadComponent: () =>
          import('./components/blog-artifact-viewer/blog-artifact-viewer.component').then(
            (m) => m.BlogArtifactViewerComponent,
          ),
        title: 'Artifact Viewer',
        data: { breadcrumb: 'Artifact' },
      },
      {
        path: 'software-engineering',
        loadComponent: () =>
          import('./components/software-engineering-dashboard/software-engineering-dashboard.component').then(
            (m) => m.SoftwareEngineeringDashboardComponent,
          ),
        title: 'Software Engineering',
        data: { breadcrumb: 'Software Engineering' },
      },
      {
        path: 'software-engineering/planning-v3',
        loadComponent: () =>
          import('./components/planning-v3-page/planning-v3-page.component').then((m) => m.PlanningV3PageComponent),
        title: 'Planning',
        data: { breadcrumb: 'Planning' },
      },
      {
        path: 'software-engineering/coding-team',
        loadComponent: () =>
          import('./components/coding-team-page/coding-team-page.component').then((m) => m.CodingTeamPageComponent),
        title: 'Coding Team',
        data: { breadcrumb: 'Coding Team' },
      },
      {
        path: 'software-engineering/code-review',
        loadComponent: () =>
          import('./components/code-review-panel/code-review-panel.component').then((m) => m.CodeReviewPanelComponent),
        title: 'Code Review',
        data: { breadcrumb: 'Code Review' },
      },
      {
        path: 'software-engineering/planning-v2/jobs/:jobId/artifacts/:artifactName',
        loadComponent: () =>
          import('./components/planning-artifact-detail/planning-artifact-detail.component').then(
            (m) => m.PlanningArtifactDetailComponent,
          ),
        title: 'Planning Artifact',
        data: { breadcrumb: 'Artifact' },
      },
      {
        path: 'market-research',
        loadComponent: () =>
          import('./components/market-research-dashboard/market-research-dashboard.component').then(
            (m) => m.MarketResearchDashboardComponent,
          ),
        title: 'Market Research',
        data: { breadcrumb: 'Market Research' },
      },
      {
        path: 'soc2-compliance',
        loadComponent: () =>
          import('./components/soc2-compliance-dashboard/soc2-compliance-dashboard.component').then(
            (m) => m.Soc2ComplianceDashboardComponent,
          ),
        title: 'SOC2 Compliance',
        data: { breadcrumb: 'SOC2 Compliance' },
      },
      {
        path: 'social-marketing',
        loadComponent: () =>
          import('./components/social-marketing-dashboard/social-marketing-dashboard.component').then(
            (m) => m.SocialMarketingDashboardComponent,
          ),
        title: 'Social Marketing',
        data: { breadcrumb: 'Social Marketing' },
      },
      {
        path: 'branding',
        loadComponent: () =>
          import('./components/branding-dashboard/branding-dashboard.component').then((m) => m.BrandingDashboardComponent),
        title: 'Branding',
        data: { breadcrumb: 'Branding' },
      },
      {
        path: 'personal-assistant',
        loadComponent: () =>
          import('./components/personal-assistant-dashboard/personal-assistant-dashboard.component').then(
            (m) => m.PersonalAssistantDashboardComponent,
          ),
        title: 'Personal Assistant',
        data: { breadcrumb: 'Personal Assistant' },
      },
      {
        path: 'accessibility',
        loadComponent: () =>
          import('./components/accessibility-dashboard/accessibility-dashboard.component').then(
            (m) => m.AccessibilityDashboardComponent,
          ),
        title: 'Accessibility Audit',
        data: { breadcrumb: 'Accessibility Audit' },
      },
      {
        path: 'agent-studio',
        loadComponent: () =>
          import('./components/agent-studio-shell/agent-studio-shell.component').then(
            (m) => m.AgentStudioShellComponent,
          ),
        title: 'Agent Studio',
        data: { breadcrumb: 'Agent Studio' },
      },
      {
        path: 'agent-console',
        loadComponent: () =>
          import('./components/agent-console/agent-console.component').then((m) => m.AgentConsoleComponent),
        title: 'Agent Console',
        data: { breadcrumb: 'Agent Console' },
      },
      { path: 'agent-provisioning', redirectTo: '/agent-console', pathMatch: 'full' },
      {
        path: 'ai-systems',
        loadComponent: () =>
          import('./components/ai-systems-dashboard/ai-systems-dashboard.component').then(
            (m) => m.AISystemsDashboardComponent,
          ),
        title: 'AI Systems',
        data: { breadcrumb: 'AI Systems' },
      },
      {
        path: 'investment',
        loadComponent: () =>
          import('./components/investment-dashboard/investment-dashboard.component').then(
            (m) => m.InvestmentDashboardComponent,
          ),
        title: 'Investment',
        data: { breadcrumb: 'Investment' },
      },
      {
        path: 'investment/advisor',
        loadComponent: () =>
          import('./components/investment-dashboard/investment-dashboard.component').then(
            (m) => m.InvestmentDashboardComponent,
          ),
        title: 'Investment Advisor',
        data: { investmentFocus: 'advisor', breadcrumb: 'Advisor & IPS' },
      },
      {
        path: 'investment/strategy-lab',
        loadComponent: () =>
          import('./components/investment-strategy-lab-page/investment-strategy-lab-page.component').then(
            (m) => m.InvestmentStrategyLabPageComponent,
          ),
        title: 'Strategy Lab',
        data: { breadcrumb: 'Strategy Lab' },
      },
      {
        path: 'integrations',
        loadComponent: () =>
          import('./components/integrations-dashboard/integrations-dashboard.component').then(
            (m) => m.IntegrationsDashboardComponent,
          ),
        canDeactivate: [unsavedChangesGuard],
        title: 'Integrations',
        data: { breadcrumb: 'Integrations' },
      },
      {
        path: 'user-profile',
        loadComponent: () =>
          import('./components/user-profile/user-profile.component').then((m) => m.UserProfileComponent),
        canDeactivate: [unsavedChangesGuard],
        title: 'User Profile',
        data: { breadcrumb: 'User Profile' },
      },
      {
        path: 'llm-config',
        loadComponent: () =>
          import('./components/llm-config-dashboard/llm-config-dashboard.component').then(
            (m) => m.LlmConfigDashboardComponent,
          ),
        canDeactivate: [unsavedChangesGuard],
        title: 'LLM Provider',
        data: { breadcrumb: 'LLM Provider' },
      },
      {
        path: 'sales',
        loadComponent: () =>
          import('./components/sales-dashboard/sales-dashboard.component').then((m) => m.SalesDashboardComponent),
        title: 'Sales',
        data: { breadcrumb: 'Sales' },
      },
      {
        path: 'nutrition',
        loadComponent: () =>
          import('./components/nutrition-dashboard/nutrition-dashboard.component').then((m) => m.NutritionDashboardComponent),
        title: 'Nutritionist',
        data: { breadcrumb: 'Nutritionist' },
      },
      {
        path: 'agentic-teams',
        loadComponent: () =>
          import('./components/agentic-team-dashboard/agentic-team-dashboard.component').then(
            (m) => m.AgenticTeamDashboardComponent,
          ),
        title: 'Agentic Teams',
        data: { breadcrumb: 'Agentic Teams' },
      },
      {
        path: 'startup-advisor',
        loadComponent: () =>
          import('./components/startup-advisor-dashboard/startup-advisor-dashboard.component').then(
            (m) => m.StartupAdvisorDashboardComponent,
          ),
        title: 'Startup Advisor',
        data: { breadcrumb: 'Startup Advisor' },
      },
      {
        path: 'persona-testing',
        loadComponent: () =>
          import('./components/persona-testing-dashboard/persona-testing-dashboard.component').then(
            (m) => m.PersonaTestingDashboardComponent,
          ),
        title: 'Testing Personas',
        data: { breadcrumb: 'Testing Personas' },
      },
      {
        path: 'persona-testing/audit/:runId',
        loadComponent: () =>
          import('./components/persona-test-audit-panel/persona-test-audit-panel.component').then(
            (m) => m.PersonaTestAuditPanelComponent,
          ),
        title: 'Testing Personas Audit',
        data: { breadcrumb: 'Audit' },
      },
      {
        path: 'deepthought',
        loadComponent: () =>
          import('./components/deepthought-dashboard/deepthought-dashboard.component').then(
            (m) => m.DeepthoughtDashboardComponent,
          ),
        title: 'Deepthought',
        data: { breadcrumb: 'Deepthought' },
      },
      {
        path: 'road-trip-planning',
        loadComponent: () =>
          import('./components/road-trip-planning-dashboard/road-trip-planning-dashboard.component').then(
            (m) => m.RoadTripPlanningDashboardComponent,
          ),
        title: 'Road Trip Planning',
        data: { breadcrumb: 'Road Trip Planning' },
      },
    ],
  },
  { path: '**', redirectTo: '/dashboard' },
];
