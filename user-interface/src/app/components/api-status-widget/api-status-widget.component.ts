import { Component, OnInit, inject } from '@angular/core';
import { forkJoin } from 'rxjs';
import { map, catchError } from 'rxjs/operators';
import { of } from 'rxjs';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { BloggingApiService } from '../../services/blogging-api.service';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { MarketResearchApiService } from '../../services/market-research-api.service';
import { Soc2ComplianceApiService } from '../../services/soc2-compliance-api.service';
import { SocialMarketingApiService } from '../../services/social-marketing-api.service';
import { BrandingApiService } from '../../services/branding-api.service';

interface ApiStatus {
  name: string;
  ok: boolean;
}

/**
 * Widget showing health status of all agent APIs.
 */
@Component({
  selector: 'app-api-status-widget',
  standalone: true,
  imports: [MatIconModule, MatTooltipModule],
  templateUrl: './api-status-widget.component.html',
  styleUrl: './api-status-widget.component.scss',
})
export class ApiStatusWidgetComponent implements OnInit {
  private readonly blogging = inject(BloggingApiService);
  private readonly softwareEngineering = inject(SoftwareEngineeringApiService);
  private readonly marketResearch = inject(MarketResearchApiService);
  private readonly soc2 = inject(Soc2ComplianceApiService);
  private readonly socialMarketing = inject(SocialMarketingApiService);
  private readonly branding = inject(BrandingApiService);

  statuses: ApiStatus[] = [];
  loading = true;

  /**
   * One-line status summary used as the widget's accessible name and tooltip,
   * so a keyboard user who focuses it hears the health at a glance (the
   * per-icon tooltips are mouse-only).
   */
  get summary(): string {
    if (this.loading) return 'API status: checking…';
    const healthy = this.statuses.filter((s) => s.ok).length;
    return `API status: ${healthy} of ${this.statuses.length} services healthy`;
  }

  ngOnInit(): void {
    forkJoin({
      blogging: this.blogging.health().pipe(
        map((r) => r?.status === 'ok'),
        catchError(() => of(false))
      ),
      softwareEngineering: this.softwareEngineering.health().pipe(
        map((r) => r?.status === 'ok'),
        catchError(() => of(false))
      ),
      marketResearch: this.marketResearch.health().pipe(
        map((r) => r?.status === 'ok'),
        catchError(() => of(false))
      ),
      soc2: this.soc2.health().pipe(
        map((r) => r?.status === 'ok'),
        catchError(() => of(false))
      ),
      socialMarketing: this.socialMarketing.health().pipe(
        map((r) => r?.status === 'ok'),
        catchError(() => of(false))
      ),
      branding: this.branding.health().pipe(
        map((r) => r?.status === 'ok'),
        catchError(() => of(false))
      ),
    }).subscribe((res) => {
      this.statuses = [
        { name: 'Blogging', ok: res.blogging },
        { name: 'Software Engineering', ok: res.softwareEngineering },
        { name: 'Market Research', ok: res.marketResearch },
        { name: 'SOC2 Compliance', ok: res.soc2 },
        { name: 'Social Marketing', ok: res.socialMarketing },
        { name: 'Branding', ok: res.branding },
      ];
      this.loading = false;
    });
  }
}
