import { Routes } from '@angular/router';
import { AppShellComponent } from './components/app-shell/app-shell.component';
import { JobsDashboardComponent } from './components/jobs-dashboard/jobs-dashboard.component';
import { BloggingDashboardComponent } from './components/blogging-dashboard/blogging-dashboard.component';
import { BlogArtifactViewerComponent } from './components/blog-artifact-viewer/blog-artifact-viewer.component';
import { MarketResearchDashboardComponent } from './components/market-research-dashboard/market-research-dashboard.component';
import { Soc2ComplianceDashboardComponent } from './components/soc2-compliance-dashboard/soc2-compliance-dashboard.component';
import { SocialMarketingDashboardComponent } from './components/social-marketing-dashboard/social-marketing-dashboard.component';
import { BrandingDashboardComponent } from './components/branding-dashboard/branding-dashboard.component';
import { SoftwareEngineeringDashboardComponent } from './components/software-engineering-dashboard/software-engineering-dashboard.component';
import { PlanningV2PageComponent } from './components/planning-v2-page/planning-v2-page.component';
import { PlanningV3PageComponent } from './components/planning-v3-page/planning-v3-page.component';
import { PersonalAssistantDashboardComponent } from './components/personal-assistant-dashboard/personal-assistant-dashboard.component';
import { AccessibilityDashboardComponent } from './components/accessibility-dashboard/accessibility-dashboard.component';
import { AgentProvisioningDashboardComponent } from './components/agent-provisioning-dashboard/agent-provisioning-dashboard.component';
import { AISystemsDashboardComponent } from './components/ai-systems-dashboard/ai-systems-dashboard.component';
import { InvestmentDashboardComponent } from './components/investment-dashboard/investment-dashboard.component';
import { IntegrationsDashboardComponent } from './components/integrations-dashboard/integrations-dashboard.component';

export const routes: Routes = [
  {
    path: '',
    component: AppShellComponent,
    children: [
      { path: '', redirectTo: '/dashboard', pathMatch: 'full' },
      { path: 'dashboard', component: JobsDashboardComponent },
      { path: 'blogging', component: BloggingDashboardComponent },
      { path: 'blogging/jobs/:jobId/artifacts/:artifactName', component: BlogArtifactViewerComponent },
      { path: 'software-engineering', component: SoftwareEngineeringDashboardComponent },
      { path: 'software-engineering/planning-v2', component: PlanningV2PageComponent },
      { path: 'software-engineering/planning-v3', component: PlanningV3PageComponent },
      { path: 'market-research', component: MarketResearchDashboardComponent },
      { path: 'soc2-compliance', component: Soc2ComplianceDashboardComponent },
      { path: 'social-marketing', component: SocialMarketingDashboardComponent },
      { path: 'branding', component: BrandingDashboardComponent },
      { path: 'personal-assistant', component: PersonalAssistantDashboardComponent },
      { path: 'accessibility', component: AccessibilityDashboardComponent },
      { path: 'agent-provisioning', component: AgentProvisioningDashboardComponent },
      { path: 'ai-systems', component: AISystemsDashboardComponent },
      { path: 'investment', component: InvestmentDashboardComponent },
      { path: 'integrations', component: IntegrationsDashboardComponent },
    ],
  },
  { path: '**', redirectTo: '/dashboard' },
];
