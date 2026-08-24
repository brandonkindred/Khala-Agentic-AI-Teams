import {
  Component,
  EventEmitter,
  Input,
  OnChanges,
  Output,
  SimpleChanges,
  inject,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatChipsModule } from '@angular/material/chips';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatDividerModule } from '@angular/material/divider';
import { MatListModule } from '@angular/material/list';
import { AccessibilityApiService } from '../../services/accessibility-api.service';
import type { AuditReportResponse, PatternCluster, Severity } from '../../models';
import { extractErrorDetail } from '../../shared/extract-error-detail';

@Component({
  selector: 'app-accessibility-report',
  standalone: true,
  imports: [
    CommonModule,
    MatCardModule,
    MatIconModule,
    MatButtonModule,
    MatChipsModule,
    MatProgressSpinnerModule,
    MatDividerModule,
    MatListModule,
  ],
  templateUrl: './accessibility-report.component.html',
  styleUrl: './accessibility-report.component.scss',
})
export class AccessibilityReportComponent implements OnChanges {
  private readonly api = inject(AccessibilityApiService);

  @Input() auditId: string | null = null;
  @Output() exportRequested = new EventEmitter<'json' | 'csv'>();
  @Output() viewFindings = new EventEmitter<void>();

  report: AuditReportResponse | null = null;
  loading = false;
  error: string | null = null;

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['auditId'] && this.auditId) {
      this.loadReport();
    }
  }

  loadReport(): void {
    if (!this.auditId) return;

    this.loading = true;
    this.error = null;

    this.api.getReport(this.auditId).subscribe({
      next: (res) => {
        this.report = res;
        this.loading = false;
      },
      error: (err) => {
        this.error = extractErrorDetail(err, 'Failed to load report');
        this.loading = false;
      },
    });
  }

  get complianceScore(): number {
    if (!this.report) return 0;
    const critical = this.report.by_severity['Critical'] || 0;
    const high = this.report.by_severity['High'] || 0;
    const total = this.report.total_findings;

    if (total === 0) return 100;
    if (critical > 0) return Math.max(0, 100 - critical * 20 - high * 10);
    return Math.max(0, 100 - high * 10 - (total - high) * 2);
  }

  get complianceLevel(): string {
    const score = this.complianceScore;
    if (score >= 90) return 'Excellent';
    if (score >= 70) return 'Good';
    if (score >= 50) return 'Needs Work';
    return 'Critical Issues';
  }

  get complianceColor(): string {
    const score = this.complianceScore;
    if (score >= 90) return '#4caf50';
    if (score >= 70) return '#8bc34a';
    if (score >= 50) return '#ff9800';
    return '#f44336';
  }

  getSeverityClass(severity: string): string {
    return severity.toLowerCase();
  }

  getSeverityCount(severity: Severity): number {
    return this.report?.by_severity[severity] || 0;
  }

  getPatternPriorityLabel(priority: number): string {
    if (priority <= 1) return 'P0 - Immediate';
    if (priority <= 3) return 'P1 - High';
    if (priority <= 5) return 'P2 - Medium';
    return 'P3 - Low';
  }

  getPatternPriorityClass(priority: number): string {
    if (priority <= 1) return 'priority-p0';
    if (priority <= 3) return 'priority-p1';
    if (priority <= 5) return 'priority-p2';
    return 'priority-p3';
  }

  onExport(format: 'json' | 'csv'): void {
    this.exportRequested.emit(format);
  }

  onViewFindings(): void {
    this.viewFindings.emit();
  }

  trackByPatternId(_index: number, pattern: PatternCluster): string {
    return pattern.pattern_id;
  }
}
