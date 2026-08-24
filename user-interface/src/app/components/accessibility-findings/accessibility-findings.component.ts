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
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatTableModule } from '@angular/material/table';
import { MatCheckboxModule } from '@angular/material/checkbox';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatSelectModule } from '@angular/material/select';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatMenuModule } from '@angular/material/menu';
import { MatBadgeModule } from '@angular/material/badge';
import { MatTooltipModule } from '@angular/material/tooltip';
import { SelectionModel } from '@angular/cdk/collections';
import { AccessibilityApiService } from '../../services/accessibility-api.service';
import type {
  Finding,
  FindingsListResponse,
  Severity,
  IssueType,
  FindingFilters,
} from '../../models';
import { extractErrorDetail } from '../../shared/extract-error-detail';

@Component({
  selector: 'app-accessibility-findings',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatCardModule,
    MatTableModule,
    MatCheckboxModule,
    MatChipsModule,
    MatIconModule,
    MatButtonModule,
    MatExpansionModule,
    MatSelectModule,
    MatFormFieldModule,
    MatProgressSpinnerModule,
    MatMenuModule,
    MatBadgeModule,
    MatTooltipModule,
  ],
  templateUrl: './accessibility-findings.component.html',
  styleUrl: './accessibility-findings.component.scss',
})
export class AccessibilityFindingsComponent implements OnChanges {
  private readonly api = inject(AccessibilityApiService);

  @Input() auditId: string | null = null;
  @Output() retestRequested = new EventEmitter<string[]>();
  @Output() exportRequested = new EventEmitter<'json' | 'csv'>();

  findings: Finding[] = [];
  filteredFindings: Finding[] = [];
  bySeverity: Record<string, number> = {};
  byIssueType: Record<string, number> = {};
  total = 0;

  loading = false;
  error: string | null = null;

  selection = new SelectionModel<string>(true, []);

  severityFilter: Severity[] = [];
  issueTypeFilter: IssueType[] = [];

  readonly severityOptions: Severity[] = ['Critical', 'High', 'Medium', 'Low'];
  readonly issueTypeOptions: IssueType[] = [
    'keyboard',
    'focus',
    'contrast',
    'forms',
    'structure',
    'navigation',
    'name_role_value',
    'media',
    'timing',
    'motion',
  ];

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['auditId'] && this.auditId) {
      this.loadFindings();
    }
  }

  loadFindings(): void {
    if (!this.auditId) return;

    this.loading = true;
    this.error = null;

    const filters: FindingFilters = {};
    if (this.severityFilter.length) filters.severity = this.severityFilter;
    if (this.issueTypeFilter.length) filters.issue_type = this.issueTypeFilter;

    this.api.getFindings(this.auditId, filters).subscribe({
      next: (res: FindingsListResponse) => {
        this.findings = res.findings;
        this.filteredFindings = res.findings;
        this.bySeverity = res.by_severity;
        this.byIssueType = res.by_issue_type;
        this.total = res.total;
        this.loading = false;
        this.selection.clear();
      },
      error: (err) => {
        this.error = extractErrorDetail(err, 'Failed to load findings');
        this.loading = false;
      },
    });
  }

  applyFilters(): void {
    this.loadFindings();
  }

  clearFilters(): void {
    this.severityFilter = [];
    this.issueTypeFilter = [];
    this.loadFindings();
  }

  get hasActiveFilters(): boolean {
    return this.severityFilter.length > 0 || this.issueTypeFilter.length > 0;
  }

  toggleSelection(findingId: string): void {
    this.selection.toggle(findingId);
  }

  isSelected(findingId: string): boolean {
    return this.selection.isSelected(findingId);
  }

  selectAll(): void {
    this.filteredFindings.forEach((f) => this.selection.select(f.id));
  }

  clearSelection(): void {
    this.selection.clear();
  }

  get selectedCount(): number {
    return this.selection.selected.length;
  }

  onRetest(): void {
    if (this.selection.selected.length > 0) {
      this.retestRequested.emit(this.selection.selected);
    }
  }

  onExport(format: 'json' | 'csv'): void {
    this.exportRequested.emit(format);
  }

  getSeverityClass(severity: Severity): string {
    return severity.toLowerCase();
  }

  getSeverityIcon(severity: Severity): string {
    switch (severity) {
      case 'Critical':
        return 'error';
      case 'High':
        return 'warning';
      case 'Medium':
        return 'info';
      case 'Low':
        return 'help_outline';
      default:
        return 'help';
    }
  }

  getIssueTypeLabel(issueType: IssueType): string {
    return issueType.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  }

  getWcagCriteria(finding: Finding): string {
    return finding.wcag_mappings.map((m) => m.sc).join(', ') || 'N/A';
  }

  trackByFindingId(_index: number, finding: Finding): string {
    return finding.id;
  }
}
