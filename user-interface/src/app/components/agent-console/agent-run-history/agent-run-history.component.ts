import {
  Component,
  EventEmitter,
  Input,
  OnChanges,
  Output,
  SimpleChanges,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatMenuModule } from '@angular/material/menu';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTooltipModule } from '@angular/material/tooltip';
import { AgentRunnerApiService } from '../../../services/agent-runner-api.service';
import type { RunSummary } from '../../../models/agent-history.model';

/**
 * Small panel that renders the last N runs for an agent. Clicking a row
 * emits ``loadRun`` so the Runner can swap its input + output panes.
 * The row menu exposes Compare (opens the diff dialog) and Delete.
 */
@Component({
  selector: 'app-agent-run-history',
  standalone: true,
  imports: [
    CommonModule,
    MatButtonModule,
    MatIconModule,
    MatMenuModule,
    MatProgressSpinnerModule,
    MatTooltipModule,
  ],
  templateUrl: './agent-run-history.component.html',
  styleUrl: './agent-run-history.component.scss',
})
export class AgentRunHistoryComponent implements OnChanges {
  private readonly api = inject(AgentRunnerApiService);

  @Input({ required: true }) agentId!: string | null;
  /** Highlights the currently-displayed run in the list. */
  @Input() activeRunId: string | null = null;

  @Output() readonly loadRun = new EventEmitter<string>();
  @Output() readonly compareRun = new EventEmitter<RunSummary>();

  readonly runs = signal<RunSummary[]>([]);
  readonly loading = signal<boolean>(false);
  readonly error = signal<string | null>(null);
  readonly storageUnavailable = signal<boolean>(false);
  readonly nextCursor = signal<string | null>(null);
  readonly hasMore = signal<boolean>(false);

  ngOnChanges(changes: SimpleChanges): void {
    if ('agentId' in changes) {
      this.runs.set([]);
      this.nextCursor.set(null);
      this.hasMore.set(false);
      if (this.agentId) this.refresh();
    }
  }

  refresh(): void {
    if (!this.agentId) return;
    this.loading.set(true);
    this.error.set(null);
    this.storageUnavailable.set(false);
    this.api.listRuns(this.agentId, null, 20).subscribe({
      next: (rows) => {
        this.runs.set(rows);
        this.hasMore.set(rows.length >= 20);
        this.nextCursor.set(rows.length > 0 ? rows[rows.length - 1].created_at : null);
        this.loading.set(false);
      },
      error: (err) => {
        this.loading.set(false);
        if (err?.status === 503) {
          this.storageUnavailable.set(true);
        } else {
          this.error.set(err?.error?.detail ?? err?.message ?? 'Failed to load history');
        }
      },
    });
  }

  loadMore(): void {
    if (!this.agentId || !this.nextCursor()) return;
    this.loading.set(true);
    this.api.listRuns(this.agentId, this.nextCursor(), 20).subscribe({
      next: (rows) => {
        this.runs.update((existing) => [...existing, ...rows]);
        this.hasMore.set(rows.length >= 20);
        this.nextCursor.set(rows.length > 0 ? rows[rows.length - 1].created_at : null);
        this.loading.set(false);
      },
      error: () => {
        this.loading.set(false);
        this.hasMore.set(false);
      },
    });
  }

  deleteRun(run: RunSummary, event: Event): void {
    event.stopPropagation();
    if (!confirm(`Delete run ${run.trace_id.slice(0, 8)}? This can't be undone.`)) return;
    this.api.deleteRun(run.id).subscribe({
      next: () => {
        this.runs.update((rows) => rows.filter((r) => r.id !== run.id));
      },
    });
  }

  emitCompare(run: RunSummary, event: Event): void {
    event.stopPropagation();
    this.compareRun.emit(run);
  }

  relativeTime(iso: string): string {
    const then = new Date(iso).getTime();
    const now = Date.now();
    const deltaSec = Math.max(1, Math.round((now - then) / 1000));
    if (deltaSec < 60) return `${deltaSec}s ago`;
    if (deltaSec < 3600) return `${Math.round(deltaSec / 60)}m ago`;
    if (deltaSec < 86400) return `${Math.round(deltaSec / 3600)}h ago`;
    return `${Math.round(deltaSec / 86400)}d ago`;
  }
}
