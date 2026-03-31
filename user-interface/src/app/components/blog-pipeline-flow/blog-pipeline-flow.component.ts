import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatIconModule } from '@angular/material/icon';
import type { BlogJobStatusResponse } from '../../models';

export interface BlogPhaseDefinition {
  id: string;
  label: string;
  icon: string;
}

/** Ordered pipeline phases matching backend BlogPhase. */
export const BLOG_PHASES: BlogPhaseDefinition[] = [
  { id: 'research', label: 'Research', icon: 'search' },
  { id: 'planning', label: 'Planning', icon: 'account_tree' },
  { id: 'draft_initial', label: 'Writer', icon: 'edit_note' },
  { id: 'copy_edit', label: 'Copy edit', icon: 'spellcheck' },
  { id: 'fact_check', label: 'Fact check', icon: 'fact_check' },
  { id: 'compliance', label: 'Compliance', icon: 'gavel' },
  { id: 'rewrite', label: 'Rewrite', icon: 'refresh' },
  { id: 'finalize', label: 'Finalize', icon: 'check_circle' },
];

@Component({
  selector: 'app-blog-pipeline-flow',
  standalone: true,
  imports: [
    CommonModule,
    MatCardModule,
    MatProgressBarModule,
    MatIconModule,
  ],
  templateUrl: './blog-pipeline-flow.component.html',
  styleUrl: './blog-pipeline-flow.component.scss',
})
export class BlogPipelineFlowComponent {
  @Input() status: BlogJobStatusResponse | null = null;

  readonly BLOG_PHASES = BLOG_PHASES;

  /** True when status is completed: all phases show as completed. */
  private get isTerminalCompleted(): boolean {
    return this.status?.status === 'completed';
  }

  /** True when status is failed. */
  private get isTerminalFailed(): boolean {
    return this.status?.status === 'failed';
  }

  /** Index of current phase in BLOG_PHASES; -1 if unknown or missing. */
  private get currentPhaseIndex(): number {
    const phase = this.status?.phase;
    if (!phase) return -1;
    const idx = BLOG_PHASES.findIndex((p) => p.id === phase);
    return idx >= 0 ? idx : -1;
  }

  /** Index of phase to treat as "last completed" when failed (failed_phase or current). */
  private get failedPhaseIndex(): number {
    if (!this.isTerminalFailed) return -1;
    const fp = this.status?.failed_phase ?? this.status?.phase;
    if (!fp) return this.currentPhaseIndex >= 0 ? this.currentPhaseIndex : -1;
    const idx = BLOG_PHASES.findIndex((p) => p.id === fp);
    return idx >= 0 ? idx : this.currentPhaseIndex;
  }

  isPhaseCompleted(phaseId: string): boolean {
    if (!this.status) return false;
    const phaseIdx = BLOG_PHASES.findIndex((p) => p.id === phaseId);
    if (phaseIdx < 0) return false;
    if (this.isTerminalCompleted) return true;
    if (this.isTerminalFailed) {
      const lastCompleted = this.failedPhaseIndex;
      return lastCompleted >= 0 && phaseIdx < lastCompleted;
    }
    const cur = this.currentPhaseIndex;
    return cur > phaseIdx;
  }

  isCurrentPhase(phaseId: string): boolean {
    return this.status?.phase === phaseId;
  }

  isPhasePending(phaseId: string): boolean {
    return !this.isPhaseCompleted(phaseId) && !this.isCurrentPhase(phaseId);
  }

  getStatusBadge(): string {
    if (!this.status) return 'pending';
    return this.status.status ?? 'pending';
  }

  getStatusBadgeClass(): string {
    switch (this.status?.status) {
      case 'completed':
        return 'status-completed';
      case 'failed':
        return 'status-failed';
      case 'running':
        return 'status-running';
      case 'cancelled':
        return 'status-cancelled';
      default:
        return 'status-pending';
    }
  }
}
