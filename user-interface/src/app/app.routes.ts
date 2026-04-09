import { Routes } from '@angular/router';
import { AppShellComponent } from './components/app-shell/app-shell.component';
import { JobsDashboardComponent } from './components/jobs-dashboard/jobs-dashboard.component';
import { BlogLandingComponent } from './components/blog-landing/blog-landing.component';
import { BloggingDashboardComponent } from './components/blogging-dashboard/blogging-dashboard.component';
import { BlogArtifactViewerComponent } from './components/blog-artifact-viewer/blog-artifact-viewer.component';
import { MarketResearchDashboardComponent } from './components/market-research-dashboard/market-research-dashboard.component';
import { Soc2ComplianceDashboardComponent } from './components/soc2-compliance-dashboard/soc2-compliance-dashboard.component';
import { SocialMarketingDashboardComponent } from './components/social-marketing-dashboard/social-marketing-dashboard.component';
import { BrandingDashboardComponent } from './components/branding-dashboard/branding-dashboard.component';
import { SoftwareEngineeringDashboardComponent } from './components/software-engineering-dashboard/software-engineering-dashboard.component';
import { PlanningV3PageComponent } from './components/planning-v3-page/planning-v3-page.component';
import { CodingTeamPageComponent } from './components/coding-team-page/coding-team-page.component';
import { PersonalAssistantDashboardComponent } from './components/personal-assistant-dashboard/personal-assistant-dashboard.component';
import { AccessibilityDashboardComponent } from './components/accessibility-dashboard/accessibility-dashboard.component';
import { AgentProvisioningDashboardComponent } from './components/agent-provisioning-dashboard/agent-provisioning-dashboard.component';
import { AISystemsDashboardComponent } from './components/ai-systems-dashboard/ai-systems-dashboard.component';
import { InvestmentDashboardComponent } from './components/investment-dashboard/investment-dashboard.component';
import { InvestmentStrategyLabPageComponent } from './components/investment-strategy-lab-page/investment-strategy-lab-page.component';
import { IntegrationsDashboardComponent } from './components/integrations-dashboard/integrations-dashboard.component';
import { SalesDashboardComponent } from './components/sales-dashboard/sales-dashboard.component';
import { NutritionDashboardComponent } from './components/nutrition-dashboard/nutrition-dashboard.component';
import { StartupAdvisorDashboardComponent } from './components/startup-advisor-dashboard/startup-advisor-dashboard.component';
import { AgenticTeamDashboardComponent } from './components/agentic-team-dashboard/agentic-team-dashboard.component';
import { PlanningArtifactDetailComponent } from './components/planning-artifact-detail/planning-artifact-detail.component';
import { PersonaTestingDashboardComponent } from './components/persona-testing-dashboard/persona-testing-dashboard.component';
import { PersonaTestAuditPanelComponent } from './components/persona-test-audit-panel/persona-test-audit-panel.component';
import { DeepthoughtDashboardComponent } from './components/deepthought-dashboard/deepthought-dashboard.component';

export const routes: Routes = [
  {
    path: '',
    component: AppShellComponent,
    children: [
      { path: '', redirectTo: '/dashboard', pathMatch: 'full' },
      { path: 'dashboard', component: JobsDashboardComponent, data: { breadcrumb: 'Jobs Dashboard', title: 'Jobs Dashboard' } },
      { path: 'blogging', component: BlogLandingComponent, data: { breadcrumb: 'Blogging', title: 'Blogging' } },
      { path: 'blogging/dashboard', component: BloggingDashboardComponent, data: { breadcrumb: 'Pipeline Dashboard', title: 'Blogging Pipeline' } },
      { path: 'blogging/jobs/:jobId/artifacts/:artifactName', component: BlogArtifactViewerComponent, data: { breadcrumb: 'Artifact', title: 'Artifact Viewer' } },
      { path: 'software-engineering', component: SoftwareEngineeringDashboardComponent, data: { breadcrumb: 'Software Engineering', title: 'Software Engineering' } },
      { path: 'software-engineering/planning-v3', component: PlanningV3PageComponent, data: { breadcrumb: 'Planning', title: 'Planning' } },
      { path: 'software-engineering/coding-team', component: CodingTeamPageComponent, data: { breadcrumb: 'Coding Team', title: 'Coding Team' } },
      { path: 'software-engineering/planning-v2/jobs/:jobId/artifacts/:artifactName', component: PlanningArtifactDetailComponent, data: { breadcrumb: 'Artifact', title: 'Planning Artifact' } },
      { path: 'market-research', component: MarketResearchDashboardComponent, data: { breadcrumb: 'Market Research', title: 'Market Research' } },
      { path: 'soc2-compliance', component: Soc2ComplianceDashboardComponent, data: { breadcrumb: 'SOC2 Compliance', title: 'SOC2 Compliance' } },
      { path: 'social-marketing', component: SocialMarketingDashboardComponent, data: { breadcrumb: 'Social Marketing', title: 'Social Marketing' } },
      { path: 'branding', component: BrandingDashboardComponent, data: { breadcrumb: 'Branding', title: 'Branding' } },
      { path: 'personal-assistant', component: PersonalAssistantDashboardComponent, data: { breadcrumb: 'Personal Assistant', title: 'Personal Assistant' } },
      { path: 'accessibility', component: AccessibilityDashboardComponent, data: { breadcrumb: 'Accessibility Audit', title: 'Accessibility Audit' } },
      { path: 'agent-provisioning', component: AgentProvisioningDashboardComponent, data: { breadcrumb: 'Agent Provisioning', title: 'Agent Provisioning' } },
      { path: 'ai-systems', component: AISystemsDashboardComponent, data: { breadcrumb: 'AI Systems', title: 'AI Systems' } },
      { path: 'investment', component: InvestmentDashboardComponent, data: { breadcrumb: 'Investment', title: 'Investment' } },
      {
        path: 'investment/advisor',
        component: InvestmentDashboardComponent,
        data: { investmentFocus: 'advisor', breadcrumb: 'Advisor & IPS', title: 'Investment Advisor' },
      },
      { path: 'investment/strategy-lab', component: InvestmentStrategyLabPageComponent, data: { breadcrumb: 'Strategy Lab', title: 'Strategy Lab' } },
      { path: 'integrations', component: IntegrationsDashboardComponent, data: { breadcrumb: 'Integrations', title: 'Integrations' } },
      { path: 'sales', component: SalesDashboardComponent, data: { breadcrumb: 'Sales', title: 'Sales' } },
      { path: 'nutrition', component: NutritionDashboardComponent, data: { breadcrumb: 'Nutritionist', title: 'Nutritionist' } },
      { path: 'agentic-teams', component: AgenticTeamDashboardComponent, data: { breadcrumb: 'Agentic Teams', title: 'Agentic Teams' } },
      { path: 'startup-advisor', component: StartupAdvisorDashboardComponent, data: { breadcrumb: 'Startup Advisor', title: 'Startup Advisor' } },
      { path: 'persona-testing', component: PersonaTestingDashboardComponent, data: { breadcrumb: 'Persona Testing', title: 'Persona Testing' } },
      { path: 'persona-testing/audit/:runId', component: PersonaTestAuditPanelComponent, data: { breadcrumb: 'Audit', title: 'Persona Test Audit' } },
      { path: 'deepthought', component: DeepthoughtDashboardComponent, data: { breadcrumb: 'Deepthought', title: 'Deepthought' } },
    ],
  },
  { path: '**', redirectTo: '/dashboard' },
];
