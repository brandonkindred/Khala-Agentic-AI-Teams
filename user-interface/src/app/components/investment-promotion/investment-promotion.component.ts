import { Component, Input, Output, EventEmitter, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule, ReactiveFormsModule, FormBuilder, FormGroup, Validators } from '@angular/forms';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatDividerModule } from '@angular/material/divider';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { MatListModule } from '@angular/material/list';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatStepperModule } from '@angular/material/stepper';

import { InlineBannerComponent } from '../../shared/inline-banner/inline-banner.component';
import { InvestmentApiService } from '../../services/investment-api.service';
import {
  IPS,
  StrategySpec,
  PromotionDecision,
  PromotionDecisionRequest,
  PromotionStage,
} from '../../models';
import { extractErrorDetail } from '../../shared/extract-error-detail';

@Component({
  selector: 'app-investment-promotion',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    ReactiveFormsModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatCardModule,
    MatChipsModule,
    MatDividerModule,
    MatProgressBarModule,
    MatSlideToggleModule,
    MatListModule,
    MatTooltipModule,
    MatStepperModule,
    InlineBannerComponent,
  ],
  templateUrl: './investment-promotion.component.html',
  styleUrl: './investment-promotion.component.scss',
})
export class InvestmentPromotionComponent {
  @Input() ips: IPS | null = null;
  @Input() strategy: StrategySpec | null = null;
  @Output() decisionMade = new EventEmitter<PromotionDecision>();

  private readonly api = inject(InvestmentApiService);
  private readonly fb = inject(FormBuilder);

  loading = false;
  error: string | null = null;
  decision: PromotionDecision | null = null;

  form: FormGroup = this.fb.group({
    proposer_agent_id: ['strategy_agent', Validators.required],
    approver_agent_id: ['approval_agent', Validators.required],
    approver_role: ['approver'],
    approver_version: ['1.0'],
    risk_veto: [false],
    human_live_approval: [false],
  });

  readonly gateLabels: Record<string, string> = {
    separation_of_duties: 'Separation of Duties',
    risk_veto: 'Risk Veto',
    validation: 'Validation',
    ips_permission: 'IPS Permission',
    human_approval: 'Human Approval',
  };

  readonly outcomeConfig: Record<PromotionStage, { icon: string; color: string; label: string }> = {
    reject: { icon: 'cancel', color: '#f44336', label: 'Rejected' },
    revise: { icon: 'edit', color: '#ff9800', label: 'Requires Revision' },
    paper: { icon: 'description', color: '#2196f3', label: 'Paper Trading' },
    live: { icon: 'rocket_launch', color: '#4caf50', label: 'Live Trading' },
  };

  runPromotion(): void {
    if (!this.ips || !this.strategy || this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }

    this.loading = true;
    this.error = null;
    this.decision = null;

    const formValue = this.form.value;

    const request: PromotionDecisionRequest = {
      strategy_id: this.strategy.strategy_id,
      user_id: this.ips.profile.user_id,
      proposer_agent_id: formValue.proposer_agent_id,
      approver_agent_id: formValue.approver_agent_id,
      approver_role: formValue.approver_role,
      approver_version: formValue.approver_version,
      risk_veto: formValue.risk_veto,
      human_live_approval: formValue.human_live_approval,
    };

    this.api.promotionDecision(request).subscribe({
      next: (response) => {
        this.loading = false;
        this.decision = response.decision;
        this.decisionMade.emit(response.decision);
      },
      error: (err) => {
        this.loading = false;
        this.error = extractErrorDetail(err, 'Failed to run promotion decision');
      },
    });
  }

  getGateIcon(result: string): string {
    switch (result) {
      case 'pass':
        return 'check_circle';
      case 'warn':
        return 'warning';
      case 'fail':
        return 'cancel';
      default:
        return 'help';
    }
  }

  getGateClass(result: string): string {
    return `gate-${result}`;
  }

  getOutcomeConfig(outcome: PromotionStage): { icon: string; color: string; label: string } {
    return this.outcomeConfig[outcome] || this.outcomeConfig.reject;
  }
}
