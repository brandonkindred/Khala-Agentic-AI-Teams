import { Component, EventEmitter, Input, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import type { GitHubPullRequestItem } from '../../../models/integrations.model';
import type { CodeReviewSummary } from '../../../models/coding-team.model';
import { isCodingTeamTerminalStatus } from '../../../models/job-status.model';
import { InlineBannerComponent } from '../../../shared/inline-banner/inline-banner.component';
import { PendingIssueProposalsComponent } from '../pending-issue-proposals/pending-issue-proposals.component';
import type { PrReviewRecord } from '../pr-review-record.model';

/**
 * The expanded detail panel for a single pull request in the Code Review page:
 * the PR header, the Start Review action, a table of every review run on that PR
 * (status/outcome/findings/started/link), and the pending-issue-proposals list.
 *
 * Presentational and stateless: the parent panel owns the PR list, the review
 * records, the live pollers, and every write (starting a review, filing issues).
 * This component only renders what it is handed and emits the user's intent back
 * up. It reads its inputs directly under default change detection, so when the
 * parent's pollers mutate a `PrReviewRecord` in place and call `markForCheck()`,
 * this child is re-checked in the same pass and the table/badges refresh.
 *
 * Invariants:
 * - Inputs are held by reference and never mutated here — in particular the
 *   `reviews` array and its records must be the *same* objects the parent's
 *   pollers write to, or live status updates would stop.
 */
@Component({
  selector: 'app-pr-review-detail',
  standalone: true,
  imports: [
    CommonModule,
    MatIconModule,
    MatButtonModule,
    MatProgressSpinnerModule,
    InlineBannerComponent,
    PendingIssueProposalsComponent,
  ],
  templateUrl: './pr-review-detail.component.html',
  styleUrl: './pr-review-detail.component.scss',
})
export class PrReviewDetailComponent {
  /** The pull request this panel details. */
  @Input({ required: true }) pull!: GitHubPullRequestItem;

  /**
   * Every review run recorded for this PR, newest-first. Passed by reference from
   * the parent (its `reviewsFor(pull.number)`); records are mutated in place by the
   * parent's pollers, so this array must not be copied.
   */
  @Input({ required: true }) reviews!: PrReviewRecord[];

  /** Whether a Start Review request for this PR is currently in flight. */
  @Input() starting = false;

  /** The last Start Review failure for this PR, or null. */
  @Input() reviewError: string | null = null;

  /** Job ids whose "create issues" request is in flight (drives the child's spinner). */
  @Input({ required: true }) creatingIssues!: Set<string>;

  /** Per-job "create issues" failures, keyed by job id. */
  @Input({ required: true }) createIssueErrors!: Map<string, string>;

  /** Emitted (with the PR) when the user clicks Start Review. */
  @Output() startReviewRequested = new EventEmitter<GitHubPullRequestItem>();

  /** Emitted when the user asks to file issues for a run's selected proposals. */
  @Output() createIssuesRequested = new EventEmitter<{ record: PrReviewRecord; ids: string[] }>();

  /**
   * True once a single review run has reached a terminal state.
   *
   * Preconditions: `record` is a review run for this PR.
   * Postconditions: returns true iff `record.status` is terminal. Pure — no side effects.
   */
  isRecordTerminal(record: PrReviewRecord): boolean {
    return isCodingTeamTerminalStatus(record.status);
  }

  /**
   * Findings posted as standalone comments, normalized across the field rename.
   * Rows persisted before `body_findings` became `comment_findings` only carry the
   * legacy key, so fall back to it (then 0) rather than rendering a blank count.
   *
   * Preconditions: `summary` is a review summary for one of this PR's runs.
   * Postconditions: returns a non-negative comment-findings count. Pure — no side effects.
   */
  commentFindings(summary: CodeReviewSummary): number {
    return summary.comment_findings ?? summary.body_findings ?? 0;
  }

  /**
   * True when a *terminal* review has pre-existing-bug proposals to show.
   * Gated on terminal so proposals never flash mid-review, before the summary
   * (and its proposal list) is final.
   *
   * Preconditions: `record` is a review run for this PR.
   * Postconditions: returns true iff the run is terminal and its summary carries at
   * least one pending-issue proposal. Pure — no side effects.
   */
  hasProposals(record: PrReviewRecord): boolean {
    return this.isRecordTerminal(record) && (record.reviewSummary?.pending_issue_proposals?.length ?? 0) > 0;
  }

  /**
   * Whether a "create issues" request is in flight for a run.
   *
   * Preconditions: `jobId` is a review run's job id.
   * Postconditions: returns true iff `jobId` is in the `creatingIssues` input. Pure.
   */
  isCreatingIssues(jobId: string): boolean {
    return this.creatingIssues.has(jobId);
  }

  /**
   * The "create issues" failure for a run, if its last attempt failed.
   *
   * Preconditions: `jobId` is a review run's job id.
   * Postconditions: returns the stored error string for `jobId`, or null. Pure.
   */
  createIssueErrorFor(jobId: string): string | null {
    return this.createIssueErrors.get(jobId) ?? null;
  }

  /**
   * Relay a Start Review click to the parent.
   *
   * Postconditions: emits `startReviewRequested` with this panel's `pull`.
   */
  onStartReview(): void {
    this.startReviewRequested.emit(this.pull);
  }

  /**
   * Relay a "file issues" request from the proposals child to the parent.
   *
   * Preconditions: `record` is one of this PR's runs; `ids` are its selected proposal ids.
   * Postconditions: emits `createIssuesRequested` with `{ record, ids }`.
   */
  onCreateIssues(record: PrReviewRecord, ids: string[]): void {
    this.createIssuesRequested.emit({ record, ids });
  }
}
