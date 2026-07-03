import { Component, Input } from '@angular/core';
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
 * - Semantic landmark structure
 *
 * The browser tab title is set globally by `AppTitleStrategy` from each
 * route's `data.title` (WCAG 2.4.2) — this component no longer sets it, so the
 * tab title has a single authoritative source.
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
export class DashboardShellComponent {
  @Input() title = '';
  @Input() subtitle = '';
  /** Material icon name for the page header. */
  @Input() icon = '';
  /** Health check function passed to HealthIndicatorComponent. */
  @Input() healthCheck?: () => Observable<{ status?: string }>;
  @Input() healthLabel = 'API';
  /** Sub-team links shown below the header. */
  @Input() subTeams: { label: string; route: string }[] = [];
}
