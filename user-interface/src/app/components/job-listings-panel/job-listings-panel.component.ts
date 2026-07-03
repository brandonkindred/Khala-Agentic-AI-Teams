import {
  Component,
  DestroyRef,
  ElementRef,
  EventEmitter,
  OnDestroy,
  OnInit,
  Output,
  inject,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatSnackBar } from '@angular/material/snack-bar';
import { JobMatchingApiService } from '../../services/job-matching-api.service';
import { EmptyStateComponent } from '../../shared/empty-state/empty-state.component';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { JobListingCardComponent } from '../job-listing-card/job-listing-card.component';
import { deferFocus } from '../../shared/defer-focus';
import { extractErrorDetail } from '../../shared/extract-error-detail';
import { LatestOnly } from '../../shared/latest-only';
import { TRIAGED_AWAY_STATUSES } from '../../models';
import type { Listing, ListingFilter, ListingStatus } from '../../models';

/** Filter pills in display order. `active` is the inbox view. */
const FILTERS: { key: ListingFilter; label: string }[] = [
  { key: 'active', label: 'Active' },
  { key: 'favorite', label: 'Favorites' },
  { key: 'poor_fit', label: 'Poor fit' },
  { key: 'not_interested', label: 'Not interested' },
  { key: 'archived', label: 'Archived' },
  { key: 'all', label: 'All' },
];

const STATUS_VERBS: Record<ListingStatus, string> = {
  new: 'Restored to New',
  favorite: 'Added to favorites',
  not_interested: 'Marked as not interested',
  poor_fit: 'Marked as poor fit',
  archived: 'Archived',
};

/**
 * Listings management panel: aggregated listings across runs with status
 * filter pills (roving arrow-key navigation) and per-card triage actions.
 * Updates are pessimistic — the card's actions disable while the PATCH is
 * in flight and the row is replaced (or removed from the current filter)
 * from the server's response. Every status snackbar carries an Undo action
 * that PATCHes the previous status back, and keyboard focus is restored to
 * the next card (or the filter pills) when a card leaves the list.
 */
@Component({
  selector: 'app-job-listings-panel',
  standalone: true,
  imports: [
    MatIconModule,
    MatButtonModule,
    EmptyStateComponent,
    LoadingSpinnerComponent,
    JobListingCardComponent,
  ],
  templateUrl: './job-listings-panel.component.html',
  styleUrl: './job-listings-panel.component.scss',
})
export class JobListingsPanelComponent implements OnInit, OnDestroy {
  /** Emitted by the empty state's "Start a scan" CTA; the dashboard switches tabs. */
  @Output() startScanRequested = new EventEmitter<void>();
  /** Emitted by the empty state's "Set up your profile first" CTA. */
  @Output() setupProfileRequested = new EventEmitter<void>();

  private readonly api = inject(JobMatchingApiService);
  private readonly snackBar = inject(MatSnackBar);
  private readonly destroyRef = inject(DestroyRef);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  readonly filters = FILTERS;

  filter: ListingFilter = 'active';
  listings: Listing[] = [];
  counts: Record<string, number> = {};
  /** Per-pill totals derived from {@link counts}; recomputed only on change,
   *  not per pill per change-detection tick. */
  pillCounts: Record<ListingFilter, number> = {} as Record<ListingFilter, number>;
  loading = false;
  error: string | null = null;
  /** Fingerprint of the listing whose PATCH is in flight, if any. */
  pendingFingerprint: string | null = null;
  /** aria-live message announcing the result of the latest load. */
  resultAnnouncement = '';

  /**
   * "Latest load wins" guard. Comparing the *filter value* instead would
   * readmit a stale response after an A→B→A round trip (both A requests match
   * the filter, so the older one arriving last would win).
   */
  private readonly loadGuard = new LatestOnly();

  /** Pending debounced load from rapid pill switching (see {@link setFilter}). */
  private filterDebounce?: ReturnType<typeof setTimeout>;
  /** Debounce window for pill-driven loads — long enough to coalesce arrow-key
   *  roving, short enough to feel instant on a deliberate click. */
  private static readonly FILTER_DEBOUNCE_MS = 200;

  ngOnInit(): void {
    this.load();
  }

  ngOnDestroy(): void {
    if (this.filterDebounce) {
      clearTimeout(this.filterDebounce);
    }
  }

  /** Build the polite live-region text for a result count (shared by load + triage). */
  private announceCount(n: number): void {
    this.resultAnnouncement = n === 1 ? '1 listing shown' : `${n} listings shown`;
  }

  /** Assign the server counts and derive the per-pill totals once. */
  private setCounts(counts: Record<string, number>): void {
    this.counts = counts;
    this.recomputePillCounts();
  }

  /**
   * Move one listing between status buckets locally (no network) — the status
   * transition is fully known, so the pill counts can be adjusted in place
   * instead of refetching. The next full {@link load} self-corrects any drift.
   */
  private adjustCounts(from: ListingStatus, to: ListingStatus): void {
    if (from === to) {
      return;
    }
    this.counts = {
      ...this.counts,
      [from]: Math.max((this.counts[from] ?? 0) - 1, 0),
      [to]: (this.counts[to] ?? 0) + 1,
    };
    this.recomputePillCounts();
  }

  /** Derive the number shown on each filter pill from the status counts. */
  private recomputePillCounts(): void {
    const total = Object.values(this.counts).reduce((sum, n) => sum + n, 0);
    const triagedAway = TRIAGED_AWAY_STATUSES.reduce((sum, s) => sum + (this.counts[s] ?? 0), 0);
    this.pillCounts = {
      all: total,
      active: total - triagedAway,
      new: this.counts['new'] ?? 0,
      favorite: this.counts['favorite'] ?? 0,
      not_interested: this.counts['not_interested'] ?? 0,
      poor_fit: this.counts['poor_fit'] ?? 0,
      archived: this.counts['archived'] ?? 0,
    };
  }

  /** Reload the current filter (also called by the dashboard after a scan). */
  load(): void {
    const token = this.loadGuard.next();
    this.loading = true;
    this.error = null;
    this.api
      .listListings(this.filter)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (res) => {
          if (!this.loadGuard.isCurrent(token)) {
            return; // a newer load superseded this response
          }
          this.listings = res.listings;
          this.setCounts(res.counts);
          this.loading = false;
          this.announceCount(res.listings.length);
        },
        error: (err) => {
          if (!this.loadGuard.isCurrent(token)) {
            return;
          }
          this.error = extractErrorDetail(err, 'Failed to load listings.');
          this.loading = false;
        },
      });
  }

  setFilter(filter: ListingFilter): void {
    if (filter === this.filter) {
      return;
    }
    // Update the selected filter immediately (aria-checked, pill highlight and
    // focus follow synchronously) but debounce the network load so arrow-key
    // roving across the pills doesn't fire one request per keystroke.
    this.filter = filter;
    if (this.filterDebounce) {
      clearTimeout(this.filterDebounce);
    }
    this.filterDebounce = setTimeout(
      () => this.load(),
      JobListingsPanelComponent.FILTER_DEBOUNCE_MS
    );
  }

  /**
   * Roving arrow-key navigation across the filter pills (role="radiogroup").
   * Selecting follows focus; Enter/Space are handled natively by the buttons.
   */
  onPillKeydown(event: KeyboardEvent, filter: ListingFilter): void {
    const idx = FILTERS.findIndex((f) => f.key === filter);
    if (idx < 0) {
      return;
    }
    let nextIdx = idx;
    switch (event.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        nextIdx = (idx + 1) % FILTERS.length;
        break;
      case 'ArrowLeft':
      case 'ArrowUp':
        nextIdx = (idx - 1 + FILTERS.length) % FILTERS.length;
        break;
      case 'Home':
        nextIdx = 0;
        break;
      case 'End':
        nextIdx = FILTERS.length - 1;
        break;
      default:
        return;
    }
    event.preventDefault();
    this.setFilter(FILTERS[nextIdx].key);
    const pills = this.pillElements();
    pills?.[nextIdx]?.focus();
  }

  /** Count shown on a filter pill — precomputed in {@link recomputePillCounts}. */
  countFor(filter: ListingFilter): number {
    return this.pillCounts[filter] ?? 0;
  }

  onStatusChange(listing: Listing, status: ListingStatus): void {
    const previousStatus = listing.status;
    const removalIndex = this.listings.findIndex((l) => l.fingerprint === listing.fingerprint);
    this.pendingFingerprint = listing.fingerprint;
    this.api
      .updateListing(listing.fingerprint, { status })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (updated) => {
          this.pendingFingerprint = null;
          this.adjustCounts(previousStatus, updated.status);
          const removed = this.applyUpdate(updated);
          if (removed) {
            this.restoreFocus(removalIndex);
          }
          const title = updated.posting.title || 'listing';
          const company = updated.posting.company ? ` at ${updated.posting.company}` : '';
          const verb =
            status === 'new' && previousStatus === 'favorite'
              ? 'Removed from favorites'
              : STATUS_VERBS[status];
          // A longer window (keyboard/AT users can't reach an auto-dismissing
          // toast in time) plus a durable fallback: the moved card keeps a
          // Restore action under its status filter even after the toast is gone.
          const durableHint = removed ? ' — or reopen its filter and press Restore' : '';
          this.snackBar
            .open(`${verb}: ${title}${company}${durableHint}`, 'Undo', { duration: 10000 })
            .onAction()
            .pipe(takeUntilDestroyed(this.destroyRef))
            .subscribe(() => this.undoStatusChange(updated, previousStatus));
        },
        error: (err) => {
          this.pendingFingerprint = null;
          this.snackBar.open(
            extractErrorDetail(err, 'Failed to update the listing.'),
            'Dismiss',
            { duration: 5000 }
          );
        },
      });
  }

  /** PATCH the previous status back and fold the listing into the view. */
  private undoStatusChange(listing: Listing, previousStatus: ListingStatus): void {
    this.api
      .updateListing(listing.fingerprint, { status: previousStatus })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (reverted) => {
          this.adjustCounts(listing.status, reverted.status);
          const present = this.listings.some((l) => l.fingerprint === reverted.fingerprint);
          if (present) {
            this.applyUpdate(reverted);
          } else if (this.matchesFilter(reverted.status)) {
            // The row was removed from this filter by the original change;
            // reload so it reappears in its ranked position (load() also
            // re-syncs the counts from the server).
            this.load();
          }
          this.snackBar.open('Change undone.', 'Dismiss', { duration: 3000 });
        },
        error: () => {
          this.snackBar.open('Could not undo the change.', 'Dismiss', { duration: 5000 });
        },
      });
  }

  /**
   * Replace the row with the server's version, dropping it if it left the
   * filter. Returns true when the row was removed from the visible list.
   */
  private applyUpdate(updated: Listing): boolean {
    let removed = false;
    if (!this.matchesFilter(updated.status)) {
      this.listings = this.listings.filter((l) => l.fingerprint !== updated.fingerprint);
      removed = true;
    } else {
      this.listings = this.listings.map((l) =>
        l.fingerprint === updated.fingerprint ? updated : l
      );
    }
    // Keep the polite live region in step with the visible count after an
    // in-place triage (load() announces on its own; this covers the no-reload path).
    // Pill counts were already adjusted locally by the caller — no counts refetch.
    this.announceCount(this.listings.length);
    return removed;
  }

  /**
   * After a card is removed, keep keyboard users anchored: focus the card now
   * occupying the removed card's slot (or the last card), falling back to the
   * selected filter pill when the list emptied.
   */
  private restoreFocus(removedIndex: number): void {
    deferFocus(this.host.nativeElement, (root) => {
      const toggles = root.querySelectorAll<HTMLElement>('.review-toggle');
      if (toggles.length) {
        return toggles[Math.min(Math.max(removedIndex, 0), toggles.length - 1)];
      }
      return root.querySelector<HTMLElement>('.filter-pill.selected');
    });
  }

  private pillElements(): NodeListOf<HTMLElement> | null {
    return this.host.nativeElement.querySelectorAll<HTMLElement>('.filter-pill');
  }

  private matchesFilter(status: ListingStatus): boolean {
    if (this.filter === 'all') {
      return true;
    }
    if (this.filter === 'active') {
      // Active is the inbox: everything triaged away leaves it (shared with the
      // card's isTriagedAway grouping via TRIAGED_AWAY_STATUSES).
      return !TRIAGED_AWAY_STATUSES.includes(status);
    }
    return status === this.filter;
  }

  trackByFingerprint(_index: number, listing: Listing): string {
    return listing.fingerprint;
  }
}
