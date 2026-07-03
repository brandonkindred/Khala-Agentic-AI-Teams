import { Component, EventEmitter, Input, Output } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import type { Listing, ListingStatus, SubScores } from '../../models';

/** Display metadata for the six fit dimensions, in render order. */
const SUB_SCORE_LABELS: { key: keyof SubScores; label: string }[] = [
  { key: 'title_fit', label: 'Title' },
  { key: 'seniority_fit', label: 'Seniority' },
  { key: 'location_fit', label: 'Location' },
  { key: 'comp_fit', label: 'Comp' },
  { key: 'company_fit', label: 'Company' },
  { key: 'skills_fit', label: 'Skills' },
];

const STATUS_LABELS: Record<ListingStatus, string> = {
  new: 'New',
  favorite: 'Favorite',
  not_interested: 'Not interested',
  poor_fit: 'Poor fit',
  archived: 'Archived',
};

/**
 * Presentational card for one aggregated job listing: score, recommendation
 * and status badges, triage actions, and an expandable "review" detail with
 * the fit breakdown, rationale, and concerns.
 */
@Component({
  selector: 'app-job-listing-card',
  standalone: true,
  imports: [DecimalPipe, MatButtonModule, MatIconModule, MatTooltipModule],
  templateUrl: './job-listing-card.component.html',
  styleUrl: './job-listing-card.component.scss',
})
export class JobListingCardComponent {
  @Input({ required: true }) listing!: Listing;
  /** Disables actions while a status change is in flight. */
  @Input() pending = false;
  /** Hides triage actions (used in read-only run-history views). */
  @Input() readonly = false;
  @Output() statusChange = new EventEmitter<ListingStatus>();

  expanded = false;

  readonly subScoreLabels = SUB_SCORE_LABELS;

  get scorePercent(): number {
    return Math.round(this.listing.score * 100);
  }

  get statusLabel(): string {
    return STATUS_LABELS[this.listing.status] ?? this.listing.status;
  }

  get isFavorite(): boolean {
    return this.listing.status === 'favorite';
  }

  /** True for statuses whose only action is restoring to the inbox. */
  get isTriagedAway(): boolean {
    return ['not_interested', 'poor_fit', 'archived'].includes(this.listing.status);
  }

  get salaryLabel(): string {
    const { salary_min, salary_max, currency } = this.listing.posting;
    if (!salary_min && !salary_max) {
      return 'Salary undisclosed';
    }
    const fmt = (n: number) => `${Math.round(n / 1000)}k`;
    const lo = salary_min ? fmt(salary_min) : '?';
    const hi = salary_max ? fmt(salary_max) : '?';
    return `${currency || 'USD'} ${lo}–${hi}`;
  }

  subScore(key: keyof SubScores): number {
    return this.listing.sub_scores?.[key] ?? 0;
  }

  toggleExpanded(): void {
    this.expanded = !this.expanded;
  }

  /** Favorite acts as a toggle: favoriting again returns the listing to New. */
  toggleFavorite(): void {
    this.statusChange.emit(this.isFavorite ? 'new' : 'favorite');
  }

  setStatus(status: ListingStatus): void {
    this.statusChange.emit(status);
  }
}
