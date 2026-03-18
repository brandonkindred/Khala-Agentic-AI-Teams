import { Component, Input, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatChipsModule } from '@angular/material/chips';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatDividerModule } from '@angular/material/divider';
import { MatTooltipModule } from '@angular/material/tooltip';
import type {
  SalesPipelineResult,
  Prospect,
  QualificationScore,
  BANTScore,
  DealRiskSignal,
  CloseType,
  ForecastCategory,
  NurtureTouchpoint,
  OutreachChannel,
} from '../../models';

@Component({
  selector: 'app-sales-pipeline-results',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    CommonModule,
    MatExpansionModule,
    MatCardModule,
    MatIconModule,
    MatChipsModule,
    MatProgressBarModule,
    MatDividerModule,
    MatTooltipModule,
  ],
  templateUrl: './sales-pipeline-results.component.html',
  styleUrl: './sales-pipeline-results.component.scss',
})
export class SalesPipelineResultsComponent {
  @Input() result: SalesPipelineResult | null = null;

  icpScoreClass(score: number): string {
    if (score >= 0.75) return 'score-high';
    if (score >= 0.5) return 'score-mid';
    return 'score-low';
  }

  icpScoreLabel(score: number): string {
    if (score >= 0.75) return 'Strong ICP fit';
    if (score >= 0.5) return 'Moderate ICP fit';
    return 'Weak ICP fit';
  }

  bantBarColor(score: number): 'primary' | 'accent' | 'warn' {
    if (score >= 7) return 'primary';
    if (score >= 4) return 'accent';
    return 'warn';
  }

  bantPercent(score: number): number {
    return (score / 10) * 100;
  }

  meddic(q: QualificationScore): { label: string; key: keyof typeof q.meddic; checked: boolean }[] {
    return [
      { label: 'Metrics identified', key: 'metrics_identified', checked: q.meddic.metrics_identified },
      { label: 'Economic buyer known', key: 'economic_buyer_known', checked: q.meddic.economic_buyer_known },
      { label: 'Decision criteria understood', key: 'decision_criteria_understood', checked: q.meddic.decision_criteria_understood },
      { label: 'Decision process mapped', key: 'decision_process_mapped', checked: q.meddic.decision_process_mapped },
      { label: 'Pain identified', key: 'identify_pain', checked: q.meddic.identify_pain },
      { label: 'Champion found', key: 'champion_found', checked: q.meddic.champion_found },
    ];
  }

  recommendationClass(action: string): string {
    const lower = action.toLowerCase();
    if (lower.startsWith('advance')) return 'chip-advance';
    if (lower.startsWith('disqualify')) return 'chip-disqualify';
    return 'chip-nurture';
  }

  severityClass(severity: string): string {
    switch (severity) {
      case 'high': return 'severity-high';
      case 'medium': return 'severity-medium';
      default: return 'severity-low';
    }
  }

  closeTypeLabel(type: CloseType): string {
    const labels: Record<CloseType, string> = {
      assumptive: 'Assumptive Close',
      summary: 'Summary Close',
      urgency: 'Urgency Close',
      alternative_choice: 'Alternative Choice',
      sharp_angle: 'Sharp Angle',
      feel_felt_found: 'Feel / Felt / Found',
    };
    return labels[type] ?? type;
  }

  forecastLabel(cat: ForecastCategory): string {
    const labels: Record<ForecastCategory, string> = {
      pipeline: 'Pipeline',
      best_case: 'Best Case',
      commit: 'Commit',
      closed: 'Closed',
      omitted: 'Omitted',
    };
    return labels[cat] ?? cat;
  }

  channelIcon(channel: OutreachChannel): string {
    const icons: Record<OutreachChannel, string> = {
      email: 'mail',
      phone: 'phone',
      linkedin: 'person',
      video: 'videocam',
    };
    return icons[channel] ?? 'contact_mail';
  }

  outcomeScorePercent(score: number): number {
    return Math.round(score * 100);
  }

  formatCurrency(val: number): string {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(val);
  }

  stageLabel(stage: string): string {
    return stage.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  }
}
