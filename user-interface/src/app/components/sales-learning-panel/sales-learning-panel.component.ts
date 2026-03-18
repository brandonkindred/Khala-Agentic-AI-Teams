import { Component, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ReactiveFormsModule, FormBuilder, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { SalesApiService } from '../../services/sales-api.service';
import type {
  LearningInsights,
  PipelineStage,
  OutcomeResult,
  CloseType,
} from '../../models';

@Component({
  selector: 'app-sales-learning-panel',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    MatCardModule,
    MatButtonModule,
    MatIconModule,
    MatExpansionModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatProgressSpinnerModule,
    MatSnackBarModule,
  ],
  templateUrl: './sales-learning-panel.component.html',
  styleUrl: './sales-learning-panel.component.scss',
})
export class SalesLearningPanelComponent implements OnInit {
  private readonly api = inject(SalesApiService);
  private readonly fb = inject(FormBuilder);
  private readonly snackBar = inject(MatSnackBar);

  insights: LearningInsights | null = null;
  loadingInsights = false;
  refreshing = false;
  insightsError: string | null = null;

  recordingStage = false;
  recordingDeal = false;

  readonly PIPELINE_STAGES: { value: PipelineStage; label: string }[] = [
    { value: 'prospecting', label: 'Prospecting' },
    { value: 'outreach', label: 'Outreach' },
    { value: 'qualification', label: 'Qualification' },
    { value: 'nurturing', label: 'Nurturing' },
    { value: 'discovery', label: 'Discovery' },
    { value: 'proposal', label: 'Proposal' },
    { value: 'negotiation', label: 'Negotiation' },
    { value: 'closed_won', label: 'Closed Won' },
    { value: 'closed_lost', label: 'Closed Lost' },
  ];

  readonly OUTCOME_RESULTS: { value: OutcomeResult; label: string }[] = [
    { value: 'converted', label: 'Converted — moved to next stage' },
    { value: 'stalled', label: 'Stalled — no response' },
    { value: 'objection', label: 'Objection raised' },
    { value: 'disqualified', label: 'Disqualified' },
    { value: 'won', label: 'Won' },
    { value: 'lost', label: 'Lost' },
  ];

  readonly CLOSE_TYPES: { value: CloseType; label: string }[] = [
    { value: 'assumptive', label: 'Assumptive' },
    { value: 'summary', label: 'Summary' },
    { value: 'urgency', label: 'Urgency' },
    { value: 'alternative_choice', label: 'Alternative Choice' },
    { value: 'sharp_angle', label: 'Sharp Angle' },
    { value: 'feel_felt_found', label: 'Feel / Felt / Found' },
  ];

  stageForm = this.fb.group({
    company_name: ['', Validators.required],
    stage: ['' as PipelineStage, Validators.required],
    outcome: ['' as OutcomeResult, Validators.required],
    industry: [''],
    email_touch_number: [null as number | null],
    subject_line_used: [''],
    objection_text: [''],
    close_technique_used: [null as CloseType | null],
    notes: [''],
  });

  dealForm = this.fb.group({
    company_name: ['', Validators.required],
    result: ['' as OutcomeResult, Validators.required],
    final_stage_reached: ['' as PipelineStage, Validators.required],
    industry: [''],
    deal_size_usd: [null as number | null],
    win_factor: [''],
    loss_reason: [''],
    close_technique_used: [null as CloseType | null],
    objections_raised: [''],
    sales_cycle_days: [null as number | null],
    notes: [''],
  });

  ngOnInit(): void {
    this.loadInsights();
  }

  loadInsights(): void {
    this.loadingInsights = true;
    this.insightsError = null;
    this.api.getInsights().subscribe({
      next: (insights) => {
        this.insights = insights;
        this.loadingInsights = false;
      },
      error: (err) => {
        this.loadingInsights = false;
        if (err.status === 404) {
          this.insightsError = null; // 404 is expected — show empty state
        } else {
          this.insightsError = err?.error?.detail ?? 'Failed to load insights.';
        }
      },
    });
  }

  refreshInsights(): void {
    this.refreshing = true;
    this.api.refreshInsights().subscribe({
      next: (resp) => {
        this.refreshing = false;
        this.snackBar.open(
          `Insights refreshed to v${resp.insights_version} (${resp.total_outcomes_analyzed} outcomes analyzed)`,
          'Dismiss',
          { duration: 4000 }
        );
        this.loadInsights();
      },
      error: (err) => {
        this.refreshing = false;
        this.snackBar.open(err?.error?.detail ?? 'Refresh failed.', 'Dismiss', { duration: 3000 });
      },
    });
  }

  submitStageOutcome(): void {
    if (this.stageForm.invalid) { this.stageForm.markAllAsTouched(); return; }
    this.recordingStage = true;
    const v = this.stageForm.value;
    this.api.recordStageOutcome({
      company_name: v.company_name!,
      stage: v.stage as PipelineStage,
      outcome: v.outcome as OutcomeResult,
      industry: v.industry || undefined,
      email_touch_number: v.email_touch_number ?? undefined,
      subject_line_used: v.subject_line_used || undefined,
      objection_text: v.objection_text || undefined,
      close_technique_used: (v.close_technique_used as CloseType) || undefined,
      notes: v.notes || '',
    }).subscribe({
      next: () => {
        this.recordingStage = false;
        this.stageForm.reset();
        this.snackBar.open('Stage outcome recorded.', 'Dismiss', { duration: 3000 });
      },
      error: (err) => {
        this.recordingStage = false;
        this.snackBar.open(err?.error?.detail ?? 'Failed to record outcome.', 'Dismiss', { duration: 3000 });
      },
    });
  }

  submitDealOutcome(): void {
    if (this.dealForm.invalid) { this.dealForm.markAllAsTouched(); return; }
    this.recordingDeal = true;
    const v = this.dealForm.value;
    this.api.recordDealOutcome({
      company_name: v.company_name!,
      result: v.result as OutcomeResult,
      final_stage_reached: v.final_stage_reached as PipelineStage,
      industry: v.industry || undefined,
      deal_size_usd: v.deal_size_usd ?? undefined,
      win_factor: v.win_factor || undefined,
      loss_reason: v.loss_reason || undefined,
      close_technique_used: (v.close_technique_used as CloseType) || undefined,
      objections_raised: v.objections_raised ? v.objections_raised.split('\n').map((s: string) => s.trim()).filter(Boolean) : [],
      sales_cycle_days: v.sales_cycle_days ?? undefined,
      notes: v.notes || '',
    }).subscribe({
      next: () => {
        this.recordingDeal = false;
        this.dealForm.reset();
        this.snackBar.open('Deal outcome recorded.', 'Dismiss', { duration: 3000 });
      },
      error: (err) => {
        this.recordingDeal = false;
        this.snackBar.open(err?.error?.detail ?? 'Failed to record outcome.', 'Dismiss', { duration: 3000 });
      },
    });
  }

  winRatePercent(): number {
    return Math.round((this.insights?.win_rate ?? 0) * 100);
  }

  stageEntries(): { stage: string; rate: number }[] {
    const rates = this.insights?.stage_conversion_rates ?? {};
    return Object.entries(rates).map(([stage, rate]) => ({
      stage: stage.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
      rate: Math.round(rate * 100),
    }));
  }
}
