import { Component, DestroyRef, OnInit, ViewChild, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router } from '@angular/router';
import { MatTabsModule } from '@angular/material/tabs';
import { DashboardShellComponent } from '../../shared/dashboard-shell/dashboard-shell.component';
import { JobMatchingApiService } from '../../services/job-matching-api.service';
import { JobListingsPanelComponent } from '../job-listings-panel/job-listings-panel.component';
import { JobScanPanelComponent } from '../job-scan-panel/job-scan-panel.component';
import { JobProfileFormComponent } from '../job-profile-form/job-profile-form.component';

/** Tab slugs in mat-tab-group order; used as the ?tab= query-param values. */
const TABS = ['listings', 'scans', 'profile'] as const;
type DashboardTab = (typeof TABS)[number];

/**
 * Job Matching dashboard: three tabs covering the team's whole surface —
 * Listings (manage identified roles), Scan & Runs (launch scans, browse run
 * history), and Profile (the career section of the user profile). The active
 * tab is mirrored into the `?tab=` query param so deep links (e.g. the User
 * Profile Career card → `?tab=profile`) and refreshes land on the right tab.
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
export class JobMatchingDashboardComponent implements OnInit {
  @ViewChild(JobListingsPanelComponent) listingsPanel?: JobListingsPanelComponent;

  private readonly api = inject(JobMatchingApiService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);

  readonly healthCheck = () => this.api.health();

  selectedTabIndex = 0;

  ngOnInit(): void {
    this.route.queryParams.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((params) => {
      const idx = TABS.indexOf(params['tab'] as DashboardTab);
      if (idx >= 0 && idx !== this.selectedTabIndex) {
        this.selectedTabIndex = idx;
      }
    });
  }

  /** Reflect the active tab in the URL without polluting browser history. */
  onTabIndexChange(index: number): void {
    this.selectedTabIndex = index;
    this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { tab: TABS[index] ?? TABS[0] },
      queryParamsHandling: 'merge',
      replaceUrl: true,
    });
  }

  /** The Listings empty state asked to start a scan — switch to the Scan tab. */
  onStartScanRequested(): void {
    this.onTabIndexChange(TABS.indexOf('scans'));
  }

  /** A completed scan may have added listings; refresh the Listings tab. */
  onScanCompleted(): void {
    this.listingsPanel?.load();
  }
}
