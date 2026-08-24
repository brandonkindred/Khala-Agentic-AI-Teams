import { Component, Input, Output, EventEmitter, inject, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule, ReactiveFormsModule, FormBuilder, FormGroup, FormArray, Validators } from '@angular/forms';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatCardModule } from '@angular/material/card';
import { MatTableModule } from '@angular/material/table';
import { MatChipsModule } from '@angular/material/chips';
import { MatDividerModule } from '@angular/material/divider';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatExpansionModule } from '@angular/material/expansion';

import { InlineBannerComponent } from '../../shared/inline-banner/inline-banner.component';
import { InvestmentApiService } from '../../services/investment-api.service';
import {
  IPS,
  PortfolioProposal,
  PortfolioPosition,
  CreateProposalRequest,
  ValidateProposalResponse,
} from '../../models';
import { extractErrorDetail } from '../../shared/extract-error-detail';

@Component({
  selector: 'app-investment-proposal',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    ReactiveFormsModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatButtonModule,
    MatIconModule,
    MatCardModule,
    MatTableModule,
    MatChipsModule,
    MatDividerModule,
    MatProgressBarModule,
    MatTooltipModule,
    MatExpansionModule,
    InlineBannerComponent,
  ],
  templateUrl: './investment-proposal.component.html',
  styleUrl: './investment-proposal.component.scss',
})
export class InvestmentProposalComponent implements OnInit {
  @Input() ips: IPS | null = null;
  @Input() existingProposal: PortfolioProposal | null = null;
  @Output() proposalCreated = new EventEmitter<PortfolioProposal>();

  private readonly api = inject(InvestmentApiService);
  private readonly fb = inject(FormBuilder);

  readonly assetClasses = ['equities', 'bonds', 'crypto', 'options', 'real_estate', 'commodities', 'cash', 'alternatives'];
  readonly displayedColumns = ['symbol', 'asset_class', 'weight_pct', 'rationale', 'actions'];

  loading = false;
  validating = false;
  error: string | null = null;

  currentProposal: PortfolioProposal | null = null;
  validationResult: ValidateProposalResponse | null = null;

  form: FormGroup = this.fb.group({
    objective: ['', Validators.required],
    expected_return_pct: [null],
    expected_volatility_pct: [null],
    expected_max_drawdown_pct: [null],
    assumptions: [''],
    positions: this.fb.array([]),
  });

  get positionsArray(): FormArray {
    return this.form.get('positions') as FormArray;
  }

  get positionsData(): PortfolioPosition[] {
    return this.positionsArray.value as PortfolioPosition[];
  }

  get totalWeight(): number {
    return this.positionsData.reduce((sum, p) => sum + (p.weight_pct || 0), 0);
  }

  get allocationByClass(): { assetClass: string; weight: number }[] {
    const byClass: Record<string, number> = {};
    for (const pos of this.positionsData) {
      byClass[pos.asset_class] = (byClass[pos.asset_class] || 0) + pos.weight_pct;
    }
    return Object.entries(byClass)
      .map(([assetClass, weight]) => ({ assetClass, weight }))
      .sort((a, b) => b.weight - a.weight);
  }

  ngOnInit(): void {
    if (this.existingProposal) {
      this.currentProposal = this.existingProposal;
      this.populateForm(this.existingProposal);
    }
  }

  populateForm(proposal: PortfolioProposal): void {
    this.form.patchValue({
      objective: proposal.objective,
      expected_return_pct: proposal.expected_return_pct,
      expected_volatility_pct: proposal.expected_volatility_pct,
      expected_max_drawdown_pct: proposal.expected_max_drawdown_pct,
      assumptions: proposal.assumptions.join('\n'),
    });

    this.positionsArray.clear();
    for (const pos of proposal.positions) {
      this.addPosition(pos);
    }
  }

  addPosition(pos?: Partial<PortfolioPosition>): void {
    const group = this.fb.group({
      symbol: [pos?.symbol || '', Validators.required],
      asset_class: [pos?.asset_class || 'equities', Validators.required],
      weight_pct: [pos?.weight_pct || 0, [Validators.required, Validators.min(0), Validators.max(100)]],
      rationale: [pos?.rationale || ''],
    });
    this.positionsArray.push(group);
  }

  removePosition(index: number): void {
    this.positionsArray.removeAt(index);
  }

  async createProposal(): Promise<void> {
    if (!this.ips || this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }

    this.loading = true;
    this.error = null;

    const formValue = this.form.value;

    const request: CreateProposalRequest = {
      prepared_by: 'ui_user',
      user_id: this.ips.profile.user_id,
      objective: formValue.objective,
      positions: formValue.positions,
      expected_return_pct: formValue.expected_return_pct,
      expected_volatility_pct: formValue.expected_volatility_pct,
      expected_max_drawdown_pct: formValue.expected_max_drawdown_pct,
      assumptions: formValue.assumptions ? formValue.assumptions.split('\n').filter((s: string) => s.trim()) : [],
    };

    this.api.createProposal(request).subscribe({
      next: (response) => {
        this.loading = false;
        this.currentProposal = response.proposal;
        this.proposalCreated.emit(response.proposal);
      },
      error: (err) => {
        this.loading = false;
        this.error = extractErrorDetail(err, 'Failed to create proposal');
      },
    });
  }

  validateProposal(): void {
    if (!this.currentProposal || !this.ips) return;

    this.validating = true;
    this.validationResult = null;

    this.api
      .validateProposal(this.currentProposal.proposal_id, {
        user_id: this.ips.profile.user_id,
      })
      .subscribe({
        next: (result) => {
          this.validating = false;
          this.validationResult = result;
        },
        error: (err) => {
          this.validating = false;
          this.error = extractErrorDetail(err, 'Failed to validate proposal');
        },
      });
  }
}
