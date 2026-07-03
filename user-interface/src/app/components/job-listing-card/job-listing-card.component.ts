import { Component, EventEmitter, Input, Output } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatMenuModule } from '@angular/material/menu';
import { MatTooltipModule } from '@angular/material/tooltip';
import type { Listing, ListingStatus, SubScores } from '../../models';

/** Display metadata for the six fit dimensions, in render order. Labels are
 *  spelled out so a screen reader (and a non-expert) reads them plainly rather
 *  than abbreviations like "Comp". */
const SUB_SCORE_LABELS: { key: keyof SubScores; label: string }[] = [
  { key: 'title_fit', label: 'Title' },
  { key: 'seniority_fit', label: 'Seniority' },
  { key: 'location_fit', label: 'Location' },
  { key: 'comp_fit', label: 'Compensation' },
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

/** Human labels for the ranker's recommendation enum (raw value drives styling). */
const REC_LABELS: Record<string, string> = {
  apply: 'Apply',
  maybe: 'Worth a look',
  skip: 'Skip',
};

/**
 * Presentational card for one aggregated job listing: score, recommendation
 * and status badges, triage actions, and an expandable "review" detail with
 * the fit breakdown, rationale, and concerns.
 */
@Component({
  selector: 'app-job-listing-card',
  standalone: true,
  imports: [DecimalPipe, MatButtonModule, MatIconModule, MatMenuModule, MatTooltipModule],
  templateUrl: './job-listing-card.component.html',
  styleUrl: './job-listing-card.component.scss',
  // Each card is one item of the panel's role="list" so AT announces
  // "list, N items" and the position of each ranked role.
  host: { role: 'listitem' },
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

  /** Stable id for the expandable detail region (aria-controls target). */
  get detailId(): string {
    return `listing-detail-${this.listing.fingerprint}`;
  }

  get statusLabel(): string {
    return STATUS_LABELS[this.listing.status] ?? this.listing.status;
  }

  /** Human recommendation label (falls back to the raw enum if unmapped). */
  get recommendationLabel(): string {
    return REC_LABELS[this.listing.recommendation] ?? this.listing.recommendation;
  }

  /** The listing's title for parameterizing per-card control accessible names. */
  get titleForLabel(): string {
    return this.listing.posting.title || 'this listing';
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
