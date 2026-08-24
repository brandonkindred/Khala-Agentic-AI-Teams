import { Component, OnInit, OnDestroy, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatChipsModule } from '@angular/material/chips';
import { MatBadgeModule } from '@angular/material/badge';
import { MatDividerModule } from '@angular/material/divider';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatListModule } from '@angular/material/list';
import { MatTooltipModule } from '@angular/material/tooltip';
import { interval, Subscription } from 'rxjs';
import { startWith, switchMap } from 'rxjs/operators';

import { InlineBannerComponent } from '../../shared/inline-banner/inline-banner.component';
import { InvestmentApiService } from '../../services/investment-api.service';
import {
  WorkflowStatusResponse,
  QueuesResponse,
  QueueItem,
  QUEUE_NAMES,
  WorkflowMode,
} from '../../models';
import { extractErrorDetail } from '../../shared/extract-error-detail';

@Component({
  selector: 'app-investment-workflow',
  standalone: true,
  imports: [
    CommonModule,
    MatCardModule,
    MatIconModule,
    MatButtonModule,
    MatChipsModule,
    MatBadgeModule,
    MatDividerModule,
    MatProgressSpinnerModule,
    MatExpansionModule,
    MatListModule,
    MatTooltipModule,
    InlineBannerComponent,
  ],
  templateUrl: './investment-workflow.component.html',
  styleUrl: './investment-workflow.component.scss',
})
export class InvestmentWorkflowComponent implements OnInit, OnDestroy {
  private readonly api = inject(InvestmentApiService);

  readonly queueNames = QUEUE_NAMES;

  readonly queueConfig: Record<string, { icon: string; label: string; description: string }> = {
    research: {
      icon: 'science',
      label: 'Research',
      description: 'Market research and analysis tasks',
    },
    portfolio_design: {
      icon: 'pie_chart',
      label: 'Portfolio Design',
      description: 'Portfolio construction and optimization',
    },
    validation: {
      icon: 'fact_check',
      label: 'Validation',
      description: 'Strategy backtesting and validation',
    },
    promotion: {
      icon: 'gavel',
      label: 'Promotion',
      description: 'Promotion gate decisions',
    },
    execution: {
      icon: 'swap_horiz',
      label: 'Execution',
      description: 'Trade execution and order management',
    },
    escalation: {
      icon: 'priority_high',
      label: 'Escalation',
      description: 'Items requiring human review',
    },
  };

  readonly modeConfig: Record<WorkflowMode, { icon: string; color: string; label: string }> = {
    advisory: { icon: 'lightbulb', color: '#9c27b0', label: 'Advisory' },
    paper: { icon: 'description', color: '#2196f3', label: 'Paper Trading' },
    live: { icon: 'rocket_launch', color: '#4caf50', label: 'Live Trading' },
    monitor_only: { icon: 'visibility', color: '#607d8b', label: 'Monitor Only' },
  };

  loading = true;
  error: string | null = null;

  workflowStatus: WorkflowStatusResponse | null = null;
  queues: QueuesResponse | null = null;

  private pollSub?: Subscription;

  ngOnInit(): void {
    this.startPolling();
  }

  ngOnDestroy(): void {
    this.pollSub?.unsubscribe();
  }

  startPolling(): void {
    this.pollSub = interval(10000)
      .pipe(
        startWith(0),
        switchMap(() => this.api.getWorkflowStatus())
      )
      .subscribe({
        next: (status) => {
          this.workflowStatus = status;
          this.loading = false;
          this.loadQueues();
        },
        error: (err) => {
          this.error = extractErrorDetail(err, 'Failed to load workflow status');
          this.loading = false;
        },
      });
  }

  loadQueues(): void {
    this.api.getQueues().subscribe({
      next: (queues) => {
        this.queues = queues;
      },
      error: (err) => {
        console.error('Failed to load queues:', err);
      },
    });
  }

  refresh(): void {
    this.loading = true;
    this.api.getWorkflowStatus().subscribe({
      next: (status) => {
        this.workflowStatus = status;
        this.loading = false;
        this.loadQueues();
      },
      error: (err) => {
        this.error = extractErrorDetail(err, 'Failed to refresh');
        this.loading = false;
      },
    });
  }

  getQueueItems(queueName: string): QueueItem[] {
    return this.queues?.queues[queueName] || [];
  }

  getQueueCount(queueName: string): number {
    return this.workflowStatus?.queue_counts[queueName] || 0;
  }

  getModeConfig(mode: WorkflowMode): { icon: string; color: string; label: string } {
    return this.modeConfig[mode] || this.modeConfig.monitor_only;
  }

  getPriorityClass(priority: string): string {
    return `priority-${priority}`;
  }
}
