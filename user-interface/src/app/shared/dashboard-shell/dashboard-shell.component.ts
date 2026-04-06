import { Component, inject, Input, OnChanges, OnInit } from '@angular/core';
import { Title } from '@angular/platform-browser';
import { RouterLink } from '@angular/router';
import { MatIconModule } from '@angular/material/icon';
import { Observable } from 'rxjs';
import { HealthIndicatorComponent } from '../../components/health-indicator/health-indicator.component';

/**
 * Unified layout wrapper for all team dashboards.
 *
 * Provides consistent:
 * - Page title (h1) + subtitle
 * - Health indicator slot
 * - Sub-team navigation links
 * - Browser tab title management (WCAG 2.4.2)
 * - Semantic landmark structure
 *
 * Content projection slots:
 * - `[dashboardActions]` → header action buttons
 * - `[dashboardEmpty]`   → empty state content
 * - default              → main body content
 */
@Component({
  selector: 'app-dashboard-shell',
  standalone: true,
  imports: [MatIconModule, RouterLink, HealthIndicatorComponent],
  templateUrl: './dashboard-shell.component.html',
  styleUrl: './dashboard-shell.component.scss',
})
export class DashboardShellComponent implements OnInit, OnChanges {
  @Input() title = '';
  @Input() subtitle = '';
  /** Material icon name for the page header. */
  @Input() icon = '';
  /** Health check function passed to HealthIndicatorComponent. */
  @Input() healthCheck?: () => Observable<{ status?: string }>;
  @Input() healthLabel = 'API';
  /** Sub-team links shown below the header. */
  @Input() subTeams: { label: string; route: string }[] = [];

  private titleService = inject(Title);

  ngOnInit(): void {
    this.updatePageTitle();
  }

  ngOnChanges(): void {
    this.updatePageTitle();
  }

  private updatePageTitle(): void {
    if (this.title) {
      this.titleService.setTitle(`${this.title} | Khala`);
    }
  }
}
