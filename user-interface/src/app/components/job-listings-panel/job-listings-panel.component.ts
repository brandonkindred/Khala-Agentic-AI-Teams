import { Component, DestroyRef, OnInit, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatSnackBar } from '@angular/material/snack-bar';
import { JobMatchingApiService } from '../../services/job-matching-api.service';
import { EmptyStateComponent } from '../../shared/empty-state/empty-state.component';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { JobListingCardComponent } from '../job-listing-card/job-listing-card.component';
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
 * filter pills and per-card triage actions (favorite / poor fit /
 * not interested / archive). Updates are pessimistic — the card's actions
 * disable while the PATCH is in flight and the row is replaced (or removed
 * from the current filter) from the server's response.
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
export class JobListingsPanelComponent implements OnInit {
  private readonly api = inject(JobMatchingApiService);
  private readonly snackBar = inject(MatSnackBar);
  private readonly destroyRef = inject(DestroyRef);

  readonly filters = FILTERS;

  filter: ListingFilter = 'active';
  listings: Listing[] = [];
  counts: Record<string, number> = {};
  loading = false;
  error: string | null = null;
  /** Fingerprint of the listing whose PATCH is in flight, if any. */
  pendingFingerprint: string | null = null;

  ngOnInit(): void {
    this.load();
  }

  /** Reload the current filter (also called by the dashboard after a scan). */
  load(): void {
    this.loading = true;
    this.error = null;
    this.api
      .listListings(this.filter)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (res) => {
          this.listings = res.listings;
          this.counts = res.counts;
          this.loading = false;
        },
        error: (err) => {
          this.error = err?.error?.detail ?? err?.message ?? 'Failed to load listings.';
          this.loading = false;
        },
      });
  }

  setFilter(filter: ListingFilter): void {
    if (filter === this.filter) {
      return;
    }
    this.filter = filter;
    this.load();
  }

  /** Count shown on a filter pill; `active` and `all` are derived. */
  countFor(filter: ListingFilter): number {
    const total = Object.values(this.counts).reduce((sum, n) => sum + n, 0);
    if (filter === 'all') {
      return total;
    }
    if (filter === 'active') {
      return total - (this.counts['archived'] ?? 0) - (this.counts['not_interested'] ?? 0);
    }
    return this.counts[filter] ?? 0;
  }

  onStatusChange(listing: Listing, status: ListingStatus): void {
    this.pendingFingerprint = listing.fingerprint;
    this.api
      .updateListing(listing.fingerprint, { status })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (updated) => {
          this.pendingFingerprint = null;
          this.applyUpdate(updated);
          const title = updated.posting.title || 'listing';
          const company = updated.posting.company ? ` at ${updated.posting.company}` : '';
          const verb =
            status === 'new' && listing.status === 'favorite'
              ? 'Removed from favorites'
              : STATUS_VERBS[status];
          this.snackBar.open(`${verb}: ${title}${company}`, 'Dismiss', { duration: 3500 });
        },
        error: (err) => {
          this.pendingFingerprint = null;
          const detail = err?.error?.detail ?? err?.message ?? 'Failed to update the listing.';
          this.snackBar.open(detail, 'Dismiss', { duration: 5000 });
        },
      });
  }

  /** Replace the row with the server's version, dropping it if it left the filter. */
  private applyUpdate(updated: Listing): void {
    if (!this.matchesFilter(updated.status)) {
      this.listings = this.listings.filter((l) => l.fingerprint !== updated.fingerprint);
    } else {
      this.listings = this.listings.map((l) =>
        l.fingerprint === updated.fingerprint ? updated : l
      );
    }
    // Refresh pill counts without refetching the whole page.
    this.api
      .listListings(this.filter, 1)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (res) => (this.counts = res.counts),
        error: () => undefined, // counts are cosmetic; the next full load corrects them
      });
  }

  private matchesFilter(status: ListingStatus): boolean {
    if (this.filter === 'all') {
      return true;
    }
    if (this.filter === 'active') {
      return status !== 'archived' && status !== 'not_interested';
    }
    return status === this.filter;
  }

  trackByFingerprint(_index: number, listing: Listing): string {
    return listing.fingerprint;
  }
}
