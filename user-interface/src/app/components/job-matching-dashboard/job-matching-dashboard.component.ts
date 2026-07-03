import { Component, ViewChild, inject } from '@angular/core';
import { MatTabsModule } from '@angular/material/tabs';
import { DashboardShellComponent } from '../../shared/dashboard-shell/dashboard-shell.component';
import { JobMatchingApiService } from '../../services/job-matching-api.service';
import { JobListingsPanelComponent } from '../job-listings-panel/job-listings-panel.component';
import { JobScanPanelComponent } from '../job-scan-panel/job-scan-panel.component';
import { JobProfileFormComponent } from '../job-profile-form/job-profile-form.component';

/**
 * Job Matching dashboard: three tabs covering the team's whole surface —
 * Listings (manage identified roles), Scan & Runs (launch scans, browse run
 * history), and Profile (the career section of the user profile).
 */
@Component({
  selector: 'app-job-matching-dashboard',
  standalone: true,
  imports: [
    MatTabsModule,
    DashboardShellComponent,
    JobListingsPanelComponent,
    JobScanPanelComponent,
    JobProfileFormComponent,
  ],
  templateUrl: './job-matching-dashboard.component.html',
  styleUrl: './job-matching-dashboard.component.scss',
})
export class JobMatchingDashboardComponent {
  @ViewChild(JobListingsPanelComponent) listingsPanel?: JobListingsPanelComponent;

  private readonly api = inject(JobMatchingApiService);

  readonly healthCheck = () => this.api.health();

  /** A completed scan may have added listings; refresh the Listings tab. */
  onScanCompleted(): void {
    this.listingsPanel?.load();
  }
}
