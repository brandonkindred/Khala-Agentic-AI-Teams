import { Component, Output, EventEmitter, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ReactiveFormsModule, FormBuilder, Validators } from '@angular/forms';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatSliderModule } from '@angular/material/slider';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTooltipModule } from '@angular/material/tooltip';
import { SalesApiService } from '../../services/sales-api.service';
import type { SalesPipelineRequest, IdealCustomerProfile, PipelineStage } from '../../models';

const PIPELINE_STAGES: { value: PipelineStage; label: string }[] = [
  { value: 'prospecting', label: 'Prospecting — Identify leads' },
  { value: 'outreach', label: 'Outreach — Cold sequences' },
  { value: 'qualification', label: 'Qualification — BANT + MEDDIC scoring' },
  { value: 'nurturing', label: 'Nurturing — Long-cycle follow-up' },
  { value: 'discovery', label: 'Discovery — Call prep & demo' },
  { value: 'proposal', label: 'Proposal — Full written proposal' },
  { value: 'negotiation', label: 'Negotiation — Close strategy' },
];

function splitLines(val: string): string[] {
  return val.split('\n').map(s => s.trim()).filter(Boolean);
}

function splitCommas(val: string): string[] {
  return val.split(',').map(s => s.trim()).filter(Boolean);
}

@Component({
  selector: 'app-sales-pipeline-form',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatButtonModule,
    MatIconModule,
    MatExpansionModule,
    MatSliderModule,
    MatProgressSpinnerModule,
    MatTooltipModule,
  ],
  templateUrl: './sales-pipeline-form.component.html',
  styleUrl: './sales-pipeline-form.component.scss',
})
export class SalesPipelineFormComponent {
  @Output() pipelineStarted = new EventEmitter<string>();

  private readonly api = inject(SalesApiService);
  private readonly fb = inject(FormBuilder);

  readonly pipelineStages = PIPELINE_STAGES;

  loading = false;
  error: string | null = null;
  icpPanelOpen = false;
  advancedOpen = false;

  form = this.fb.group({
    // Product
    product_name: ['', [Validators.required, Validators.minLength(1), Validators.maxLength(200)]],
    value_proposition: ['', [Validators.required, Validators.minLength(10)]],
    company_context: [''],
    case_study_snippets: [''],

    // ICP
    icp_industry: [''],
    icp_job_titles: [''],
    icp_pain_points: [''],
    icp_size_min: [10],
    icp_size_max: [5000],
    icp_budget_range: ['$10k–$100k/yr'],
    icp_geographic_focus: [''],
    icp_tech_stack: [''],
    icp_disqualifying_traits: [''],

    // Run config
    entry_stage: ['prospecting' as PipelineStage],
    max_prospects: [5],
  });

  get maxProspectsValue(): number {
    return this.form.get('max_prospects')?.value ?? 5;
  }

  submit(): void {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }
    this.loading = true;
    this.error = null;

    const v = this.form.value;
    const icp: IdealCustomerProfile = {
      industry: splitCommas(v.icp_industry ?? ''),
      company_size_min: Number(v.icp_size_min ?? 10),
      company_size_max: Number(v.icp_size_max ?? 5000),
      job_titles: splitCommas(v.icp_job_titles ?? ''),
      pain_points: splitLines(v.icp_pain_points ?? ''),
      budget_range_usd: v.icp_budget_range ?? '$10k–$100k/yr',
      geographic_focus: splitCommas(v.icp_geographic_focus ?? ''),
      tech_stack_keywords: splitCommas(v.icp_tech_stack ?? ''),
      disqualifying_traits: splitLines(v.icp_disqualifying_traits ?? ''),
    };

    const request: SalesPipelineRequest = {
      product_name: v.product_name ?? '',
      value_proposition: v.value_proposition ?? '',
      icp,
      entry_stage: (v.entry_stage as PipelineStage) ?? 'prospecting',
      max_prospects: Number(v.max_prospects ?? 5),
      company_context: v.company_context ?? '',
      case_study_snippets: splitLines(v.case_study_snippets ?? ''),
    };

    this.api.runPipeline(request).subscribe({
      next: (resp) => {
        this.loading = false;
        this.pipelineStarted.emit(resp.job_id);
      },
      error: (err) => {
        this.loading = false;
        this.error = err?.error?.detail ?? err?.message ?? 'Pipeline failed to start.';
      },
    });
  }

  reset(): void {
    this.form.reset({
      entry_stage: 'prospecting',
      max_prospects: 5,
      icp_size_min: 10,
      icp_size_max: 5000,
      icp_budget_range: '$10k–$100k/yr',
    });
    this.error = null;
    this.icpPanelOpen = false;
  }
}
