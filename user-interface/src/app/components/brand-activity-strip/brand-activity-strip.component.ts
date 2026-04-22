import { Component, inject, input, output } from '@angular/core';
import { toObservable, toSignal } from '@angular/core/rxjs-interop';
import { CommonModule } from '@angular/common';
import { MatButtonModule } from '@angular/material/button';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { switchMap } from 'rxjs/operators';
import type { BrandActivity, BrandActivityKind, BrandActivityStatus } from '../../models';
import { BrandActivityService } from '../../services/brand-activity.service';

const KIND_LABEL: Record<BrandActivityKind, string> = {
  run: 'Run',
  research: 'Market research',
  design: 'Design assets',
};

const STATUS_SUFFIX: Record<BrandActivityStatus, (a: BrandActivity) => string> = {
  queued: () => 'queued',
  running: (a) =>
    a.phase
      ? `${a.phase}${a.progress != null ? ` · ${a.progress}%` : ''}`
      : 'running',
  completed: (a) => `completed${relative(a.completedAt)}`,
  failed: () => 'failed',
  cancelled: () => 'cancelled',
};

/**
 * Compact per-brand activity strip. Renders a `mat-chip` for every pending,
 * running, or recently-finished generate action against a single brand, with
 * retry on failure and "open artifacts" on completion. Data comes from
 * {@link BrandActivityService}; the parent handles the actual retry / open
 * behaviour via the `(retry)` and `(open)` outputs.
 */
@Component({
  selector: 'app-brand-activity-strip',
  standalone: true,
  imports: [
    CommonModule,
    MatButtonModule,
    MatChipsModule,
    MatIconModule,
    MatTooltipModule,
    MatProgressSpinnerModule,
  ],
  templateUrl: './brand-activity-strip.component.html',
  styleUrl: './brand-activity-strip.component.scss',
})
export class BrandActivityStripComponent {
  private readonly activities = inject(BrandActivityService);

  readonly brandId = input.required<string>();

  readonly retry = output<BrandActivity>();
  readonly open = output<BrandActivity>();
  readonly dismiss = output<BrandActivity>();

  readonly items = toSignal(
    toObservable(this.brandId).pipe(switchMap((id) => this.activities.forBrand(id))),
    { initialValue: [] as BrandActivity[] }
  );

  label(a: BrandActivity): string {
    return `${KIND_LABEL[a.kind]} · ${STATUS_SUFFIX[a.status](a)}`;
  }

  chipClass(a: BrandActivity): string {
    return `activity-chip activity-chip--${a.status}`;
  }

  /** Completed chips are clickable to jump to artifacts; running chips aren't. */
  isOpenable(a: BrandActivity): boolean {
    return a.status === 'completed';
  }

  isRetryable(a: BrandActivity): boolean {
    return a.status === 'failed' || a.status === 'cancelled';
  }

  isDismissable(a: BrandActivity): boolean {
    return (
      a.status === 'completed' ||
      a.status === 'failed' ||
      a.status === 'cancelled'
    );
  }

  onOpen(event: Event, a: BrandActivity): void {
    if (!this.isOpenable(a)) return;
    event.stopPropagation();
    this.open.emit(a);
  }

  onRetry(event: Event, a: BrandActivity): void {
    event.stopPropagation();
    this.retry.emit(a);
  }

  onDismiss(event: Event, a: BrandActivity): void {
    event.stopPropagation();
    this.dismiss.emit(a);
  }
}

function relative(iso?: string | null): string {
  if (!iso) return '';
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return '';
  const diffMs = Date.now() - then;
  if (diffMs < 60_000) return ' · just now';
  const mins = Math.round(diffMs / 60_000);
  if (mins < 60) return ` · ${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return ` · ${hours}h ago`;
  return '';
}
