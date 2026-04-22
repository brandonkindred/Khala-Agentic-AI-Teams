import { Component, Input, OnChanges, OnDestroy, OnInit, SimpleChanges, inject, output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatButtonModule } from '@angular/material/button';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { Subject } from 'rxjs';
import { switchMap, takeUntil } from 'rxjs/operators';
import type { BrandActivity, BrandActivityKind } from '../../models';
import { BrandActivityService } from '../../services/brand-activity.service';

const KIND_LABEL: Record<BrandActivityKind, string> = {
  run: 'Run',
  research: 'Market research',
  design: 'Design assets',
};

const KIND_ICON: Record<BrandActivityKind, string> = {
  run: 'bolt',
  research: 'travel_explore',
  design: 'palette',
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
export class BrandActivityStripComponent implements OnInit, OnChanges, OnDestroy {
  private readonly activities = inject(BrandActivityService);

  @Input({ required: true }) brandId!: string;

  readonly retry = output<BrandActivity>();
  readonly open = output<BrandActivity>();
  readonly dismiss = output<BrandActivity>();

  items: BrandActivity[] = [];

  private readonly brandIdChanges = new Subject<string>();
  private readonly destroy = new Subject<void>();

  constructor() {
    this.brandIdChanges
      .pipe(
        switchMap((id) => this.activities.forBrand(id)),
        takeUntil(this.destroy)
      )
      .subscribe((list) => (this.items = list));
  }

  ngOnInit(): void {
    if (this.brandId) {
      this.brandIdChanges.next(this.brandId);
    }
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['brandId'] && !changes['brandId'].firstChange && this.brandId) {
      this.brandIdChanges.next(this.brandId);
    }
  }

  ngOnDestroy(): void {
    this.destroy.next();
    this.destroy.complete();
  }

  trackById(_: number, a: BrandActivity): string {
    return a.id;
  }

  label(a: BrandActivity): string {
    const base = KIND_LABEL[a.kind];
    if (a.status === 'running' && a.phase) {
      const percent = typeof a.progress === 'number' ? ` · ${a.progress}%` : '';
      return `${base} · ${a.phase}${percent}`;
    }
    if (a.status === 'running') {
      return `${base} · running`;
    }
    if (a.status === 'queued') {
      return `${base} · queued`;
    }
    if (a.status === 'completed') {
      return `${base} · completed${this.relative(a.completedAt)}`;
    }
    if (a.status === 'failed') {
      return `${base} · failed`;
    }
    if (a.status === 'cancelled') {
      return `${base} · cancelled`;
    }
    return base;
  }

  icon(kind: BrandActivityKind): string {
    return KIND_ICON[kind];
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

  private relative(iso?: string | null): string {
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
}
