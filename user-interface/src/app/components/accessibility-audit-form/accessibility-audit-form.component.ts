import { Component, EventEmitter, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatSelectModule } from '@angular/material/select';
import { MatCheckboxModule } from '@angular/material/checkbox';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { COMMA, ENTER } from '@angular/cdk/keycodes';
import { MatChipInputEvent } from '@angular/material/chips';
import type {
  CreateAuditRequest,
  AuditType,
  WCAGLevel,
  SamplingStrategy,
  MobileAppTarget,
} from '../../models';

@Component({
  selector: 'app-accessibility-audit-form',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatSelectModule,
    MatCheckboxModule,
    MatChipsModule,
    MatIconModule,
    MatExpansionModule,
    MatSlideToggleModule,
  ],
  templateUrl: './accessibility-audit-form.component.html',
  styleUrl: './accessibility-audit-form.component.scss',
})
export class AccessibilityAuditFormComponent {
  @Output() submitRequest = new EventEmitter<CreateAuditRequest>();

  readonly separatorKeyCodes = [ENTER, COMMA] as const;

  auditName = '';
  auditType: AuditType = 'webpage';

  webUrl = '';
  webUrls: string[] = [];

  mobileAppPlatform: 'ios' | 'android' = 'ios';
  mobileAppName = '';
  mobileAppVersion = '';
  mobileAppBuild = '';
  mobileApps: MobileAppTarget[] = [];

  criticalJourneys: string[] = [];

  wcagLevelA = true;
  wcagLevelAA = true;
  wcagLevelAAA = false;

  authRequired = false;
  maxPages: number | null = null;
  timeboxHours: number | null = null;
  samplingStrategy: SamplingStrategy = 'journey_based';

  get canSubmit(): boolean {
    if (!this.auditName.trim()) return false;

    if (this.auditType === 'mobile') {
      return this.mobileApps.length > 0;
    }
    return this.effectiveWebUrls.length > 0;
  }

  get selectedWcagLevels(): WCAGLevel[] {
    const levels: WCAGLevel[] = [];
    if (this.wcagLevelA) levels.push('A');
    if (this.wcagLevelAA) levels.push('AA');
    if (this.wcagLevelAAA) levels.push('AAA');
    return levels;
  }

  addUrl(): void {
    const url = this.webUrl.trim();
    if (url && !this.webUrls.includes(url)) {
      this.webUrls.push(url);
      this.webUrl = '';
    }
  }

  removeUrl(url: string): void {
    this.webUrls = this.webUrls.filter((u) => u !== url);
  }

  addMobileApp(): void {
    if (this.mobileAppName.trim() && this.mobileAppVersion.trim()) {
      this.mobileApps.push({
        platform: this.mobileAppPlatform,
        name: this.mobileAppName.trim(),
        version: this.mobileAppVersion.trim(),
        build: this.mobileAppBuild.trim() || undefined,
      });
      this.mobileAppName = '';
      this.mobileAppVersion = '';
      this.mobileAppBuild = '';
    }
  }

  removeMobileApp(index: number): void {
    this.mobileApps.splice(index, 1);
  }

  addJourney(event: MatChipInputEvent): void {
    const value = (event.value || '').trim();
    if (value && !this.criticalJourneys.includes(value)) {
      this.criticalJourneys.push(value);
    }
    event.chipInput!.clear();
  }

  removeJourney(journey: string): void {
    this.criticalJourneys = this.criticalJourneys.filter((j) => j !== journey);
  }

  onSubmit(): void {
    if (!this.canSubmit) return;

    const webUrls = this.effectiveWebUrls;
    const request: CreateAuditRequest = {
      name: this.auditName.trim(),
      web_urls: this.auditType === 'mobile' ? [] : webUrls,
      mobile_apps: this.auditType === 'mobile' ? this.mobileApps : [],
      critical_journeys: this.criticalJourneys,
      wcag_levels: this.selectedWcagLevels,
      auth_required: this.authRequired,
      sampling_strategy: this.samplingStrategy,
      timebox_hours: this.timeboxHours ?? undefined,
      max_pages: this.maxPages ?? undefined,
      tech_stack: {
        web: this.auditType === 'spa' ? 'spa' : 'other',
        mobile: this.auditType === 'mobile' ? 'native' : 'other',
      },
    };

    this.submitRequest.emit(request);
  }

  private get effectiveWebUrls(): string[] {
    const typedUrl = this.webUrl.trim();
    if (!typedUrl) {
      return this.webUrls;
    }
    if (this.webUrls.includes(typedUrl)) {
      return this.webUrls;
    }
    return [...this.webUrls, typedUrl];
  }

  resetForm(): void {
    this.auditName = '';
    this.auditType = 'webpage';
    this.webUrl = '';
    this.webUrls = [];
    this.mobileApps = [];
    this.mobileAppName = '';
    this.mobileAppVersion = '';
    this.mobileAppBuild = '';
    this.criticalJourneys = [];
    this.wcagLevelA = true;
    this.wcagLevelAA = true;
    this.wcagLevelAAA = false;
    this.authRequired = false;
    this.maxPages = null;
    this.timeboxHours = null;
    this.samplingStrategy = 'journey_based';
  }
}
