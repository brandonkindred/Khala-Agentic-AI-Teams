import { Component, EventEmitter, Output, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule, ReactiveFormsModule, FormBuilder, FormGroup, FormArray, Validators } from '@angular/forms';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatButtonModule } from '@angular/material/button';
import { MatSliderModule } from '@angular/material/slider';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { MatIconModule } from '@angular/material/icon';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatDividerModule } from '@angular/material/divider';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatTooltipModule } from '@angular/material/tooltip';

import { InlineBannerComponent } from '../../shared/inline-banner/inline-banner.component';
import { InvestmentApiService } from '../../services/investment-api.service';
import {
  CreateProfileRequest,
  IPS,
  RISK_TOLERANCE_OPTIONS,
  WORKFLOW_MODE_OPTIONS,
  RiskTolerance,
  WorkflowMode,
  UserGoal,
} from '../../models';
import { extractErrorDetail } from '../../shared/extract-error-detail';

@Component({
  selector: 'app-investment-profile-form',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    ReactiveFormsModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatButtonModule,
    MatSliderModule,
    MatSlideToggleModule,
    MatIconModule,
    MatCardModule,
    MatChipsModule,
    MatDividerModule,
    MatProgressBarModule,
    MatTooltipModule,
    InlineBannerComponent,
  ],
  templateUrl: './investment-profile-form.component.html',
  styleUrl: './investment-profile-form.component.scss',
})
export class InvestmentProfileFormComponent {
  @Output() profileCreated = new EventEmitter<IPS>();
  @Output() cancelled = new EventEmitter<void>();

  private readonly api = inject(InvestmentApiService);
  private readonly fb = inject(FormBuilder);

  readonly riskOptions = RISK_TOLERANCE_OPTIONS;
  readonly workflowModeOptions = WORKFLOW_MODE_OPTIONS;

  readonly assetClassOptions = ['equities', 'bonds', 'crypto', 'options', 'real_estate', 'commodities'];
  readonly esgOptions = ['none', 'light', 'moderate', 'strict'];
  readonly rebalanceOptions = ['monthly', 'quarterly', 'semi-annual', 'annual'];

  loading = false;
  error: string | null = null;

  form: FormGroup = this.fb.group({
    user_id: ['', [Validators.required, Validators.minLength(3)]],
    risk_tolerance: ['medium', Validators.required],
    max_drawdown_tolerance_pct: [20, [Validators.required, Validators.min(0), Validators.max(100)]],
    time_horizon_years: [10, [Validators.required, Validators.min(1), Validators.max(50)]],

    annual_gross_income: [0, [Validators.required, Validators.min(0)]],
    income_stability: ['stable'],
    total_net_worth: [0, [Validators.required, Validators.min(0)]],
    investable_assets: [0, [Validators.required, Validators.min(0)]],
    monthly_savings: [0],
    annual_savings: [0],

    tax_country: ['US'],
    tax_state: [''],
    account_types: [[]],
    emergency_fund_months: [6, [Validators.min(0), Validators.max(24)]],

    excluded_asset_classes: [[]],
    excluded_industries: [[]],
    esg_preference: ['none'],
    crypto_allowed: [true],
    options_allowed: [true],
    leverage_allowed: [false],

    max_single_position_pct: [10, [Validators.min(1), Validators.max(100)]],

    live_trading_enabled: [false],
    human_approval_required_for_live: [true],
    speculative_sleeve_cap_pct: [10, [Validators.min(0), Validators.max(50)]],
    rebalance_frequency: ['quarterly'],
    default_mode: ['monitor_only'],
    notes: [''],

    goals: this.fb.array([]),
  });

  get goalsArray(): FormArray {
    return this.form.get('goals') as FormArray;
  }

  addGoal(): void {
    const goalGroup = this.fb.group({
      name: ['', Validators.required],
      target_amount: [0, [Validators.required, Validators.min(0)]],
      target_date: [''],
      priority: ['medium'],
    });
    this.goalsArray.push(goalGroup);
  }

  removeGoal(index: number): void {
    this.goalsArray.removeAt(index);
  }

  async submit(): Promise<void> {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }

    this.loading = true;
    this.error = null;

    const formValue = this.form.value;

    const request: CreateProfileRequest = {
      user_id: formValue.user_id,
      risk_tolerance: formValue.risk_tolerance as RiskTolerance,
      max_drawdown_tolerance_pct: formValue.max_drawdown_tolerance_pct,
      time_horizon_years: formValue.time_horizon_years,
      annual_gross_income: formValue.annual_gross_income,
      income_stability: formValue.income_stability,
      total_net_worth: formValue.total_net_worth,
      investable_assets: formValue.investable_assets,
      monthly_savings: formValue.monthly_savings,
      annual_savings: formValue.annual_savings,
      tax_country: formValue.tax_country,
      tax_state: formValue.tax_state,
      account_types: formValue.account_types,
      emergency_fund_months: formValue.emergency_fund_months,
      excluded_asset_classes: formValue.excluded_asset_classes,
      excluded_industries: formValue.excluded_industries,
      esg_preference: formValue.esg_preference,
      crypto_allowed: formValue.crypto_allowed,
      options_allowed: formValue.options_allowed,
      leverage_allowed: formValue.leverage_allowed,
      goals: formValue.goals as UserGoal[],
      max_single_position_pct: formValue.max_single_position_pct,
      max_asset_class_pct: {},
      live_trading_enabled: formValue.live_trading_enabled,
      human_approval_required_for_live: formValue.human_approval_required_for_live,
      speculative_sleeve_cap_pct: formValue.speculative_sleeve_cap_pct,
      rebalance_frequency: formValue.rebalance_frequency,
      default_mode: formValue.default_mode as WorkflowMode,
      notes: formValue.notes ? [formValue.notes] : [],
    };

    this.api.createProfile(request).subscribe({
      next: (response) => {
        this.loading = false;
        this.profileCreated.emit(response.ips);
      },
      error: (err) => {
        this.loading = false;
        this.error = extractErrorDetail(err, 'Failed to create profile');
      },
    });
  }

  cancel(): void {
    this.cancelled.emit();
  }
}
