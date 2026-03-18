import { Component, Input, Output, EventEmitter, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatTooltipModule } from '@angular/material/tooltip';
import type { SalesPipelineJobListItem } from '../../models';

@Component({
  selector: 'app-sales-jobs-panel',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    CommonModule,
    MatCardModule,
    MatIconModule,
    MatButtonModule,
    MatProgressBarModule,
    MatTooltipModule,
  ],
  templateUrl: './sales-jobs-panel.component.html',
  styleUrl: './sales-jobs-panel.component.scss',
})
export class SalesJobsPanelComponent {
  @Input() jobs: SalesPipelineJobListItem[] = [];
  @Input() selectedJobId: string | null = null;
  @Output() jobSelected = new EventEmitter<string>();
  @Output() jobDeleted = new EventEmitter<string>();

  get runningJobs(): SalesPipelineJobListItem[] {
    return this.jobs.filter(j => j.status === 'running' || j.status === 'pending');
  }

  get completedJobs(): SalesPipelineJobListItem[] {
    return this.jobs.filter(j => j.status !== 'running' && j.status !== 'pending');
  }

  isTerminal(job: SalesPipelineJobListItem): boolean {
    return job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled';
  }

  stageLabel(stage: string): string {
    return stage.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  }

  timeAgo(isoString?: string): string {
    if (!isoString) return '';
    const diff = Date.now() - new Date(isoString).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
  }

  selectJob(jobId: string): void {
    this.jobSelected.emit(jobId);
  }

  deleteJob(event: MouseEvent, jobId: string): void {
    event.stopPropagation();
    this.jobDeleted.emit(jobId);
  }
}
