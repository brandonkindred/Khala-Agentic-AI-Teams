import { Routes } from '@angular/router';
import { AppShellComponent } from './components/app-shell/app-shell.component';
import { BloggingDashboardComponent } from './components/blogging-dashboard/blogging-dashboard.component';
import { MarketResearchDashboardComponent } from './components/market-research-dashboard/market-research-dashboard.component';
import { Soc2ComplianceDashboardComponent } from './components/soc2-compliance-dashboard/soc2-compliance-dashboard.component';
import { SocialMarketingDashboardComponent } from './components/social-marketing-dashboard/social-marketing-dashboard.component';
import { BrandingDashboardComponent } from './components/branding-dashboard/branding-dashboard.component';
import { SoftwareEngineeringDashboardComponent } from './components/software-engineering-dashboard/software-engineering-dashboard.component';

export const routes: Routes = [
  {
    path: '',
    component: AppShellComponent,
    children: [
      { path: '', redirectTo: '/blogging', pathMatch: 'full' },
      { path: 'blogging', component: BloggingDashboardComponent },
      { path: 'software-engineering', component: SoftwareEngineeringDashboardComponent },
      { path: 'market-research', component: MarketResearchDashboardComponent },
      { path: 'soc2-compliance', component: Soc2ComplianceDashboardComponent },
      { path: 'social-marketing', component: SocialMarketingDashboardComponent },
      { path: 'branding', component: BrandingDashboardComponent },
    ],
  },
  { path: '**', redirectTo: '/blogging' },
];
