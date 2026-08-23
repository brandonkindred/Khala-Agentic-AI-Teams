import { Component, EventEmitter, Input, Output, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatDialog } from '@angular/material/dialog';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import type { GitHubPullRequestItem } from '../../../models/integrations.model';
import type { CodeReviewSummary } from '../../../models/coding-team.model';
import { isCodingTeamTerminalStatus } from '../../../models/job-status.model';
import { InlineBannerComponent } from '../../../shared/inline-banner/inline-banner.component';
import {
  CodeReviewTranscriptDialogComponent,
  type CodeReviewTranscriptDialogData,
} from '../code-review-transcript-dialog/code-review-transcript-dialog.component';
import {
  CodeReviewSystemicFindingsDialogComponent,
  type CodeReviewSystemicFindingsDialogData,
} from '../code-review-systemic-findings-dialog/code-review-systemic-findings-dialog.component';
import type { PrReviewRecord } from '../pr-review-record.model';
import { reviewDuration, severityEntries } from '../review-metrics';

/**
 * The expanded detail panel for a single pull request in the Code Review page:
 * the PR header, the Start Review action, and a table of every review run on
 * that PR (status, outcome, findings, severity, started time, duration, and
 * transcript).
 *
 * Out-of-scope (pre-existing) issue proposals are no longer displayed here;
 * they are now surfaced in the Coding Team page's Issues tab.
 *
 * Presentational and stateless: the parent panel owns the PR list, the review
 * records, the live pollers, and every write (starting a review).
 * This component only renders what it is handed and emits the user's intent back
 * up. It reads its inputs directly under default change detection, so when the
 * parent's pollers mutate a `PrReviewRecord` in place and call `markForCheck()`,
 * this child is re-checked in the same pass and the table/badges refresh.
 *
 * Do NOT add `changeDetection: ChangeDetectionStrategy.OnPush` to this component:
 * the `reviews` input is typed read-only because callers must not mutate it, but
 * the parent's live pollers *do* mutate the records that input points at, in place,
 * on the same object reference — that is precisely what keeps the table/badges live.
 * OnPush only re-checks a component when an `@Input()`'s reference changes, so it
 * would stop seeing those in-place updates and the table would silently freeze
 * mid-poll.
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
  ],
  templateUrl: './pr-review-detail.component.html',
  styleUrl: './pr-review-detail.component.scss',
})
export class PrReviewDetailComponent {
  private readonly dialog = inject(MatDialog);
  /** The pull request this panel details. */
  @Input({ required: true }) pull!: GitHubPullRequestItem;

  /**
   * Every review run recorded for this PR, newest-first. Passed by reference from
   * the parent (its `reviewsFor(pull.number)`); records are mutated in place by the
   * parent's pollers, so this array must not be copied. Read-only here — this panel
   * only renders it and never mutates it.
   */
  @Input({ required: true }) reviews!: readonly PrReviewRecord[];

  /** Whether a Start Review request for this PR is currently in flight. */
  @Input() starting = false;

  /** The last Start Review failure for this PR, or null. */
  @Input() reviewError: string | null = null;

  /** Emitted (with the PR) when the user clicks Start Review. */
  @Output() startReviewRequested = new EventEmitter<GitHubPullRequestItem>();

  // Per-review metric helpers are pure functions in `review-metrics.ts` (unit-tested
  // there in isolation). Exposed as fields so the template calls them unchanged.
  readonly severityEntries = severityEntries;
  readonly reviewDuration = reviewDuration;

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
   * Count of systemic/cross-cutting findings synthesized for a review run.
   * Absent on reviews run before this feature, or when synthesis found no
   * genuine cross-cutting pattern — both render as 0.
   *
   * Preconditions: `summary` is a review summary for one of this PR's runs.
   * Postconditions: returns a non-negative count. Pure — no side effects.
   */
  systemicFindingsCount(summary: CodeReviewSummary): number {
    return summary.systemic_findings?.length ?? 0;
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
   * Open the read-only transcript dialog for a terminal review run.
   *
   * Preconditions: `record` is one of this PR's runs and `isRecordTerminal(record)`
   * is true (the "View Transcript" action is only rendered once a run completes).
   * Postconditions: opens `CodeReviewTranscriptDialogComponent`, which fetches and
   * renders the run's durable transcript itself; this method has no return value.
   */
  onViewTranscript(record: PrReviewRecord): void {
    const data: CodeReviewTranscriptDialogData = {
      owner: record.owner,
      repo: record.repo,
      jobId: record.jobId,
    };
    this.dialog.open(CodeReviewTranscriptDialogComponent, {
      data,
      width: '800px',
      maxWidth: '95vw',
    });
  }

  /**
   * Open the read-only systemic/cross-cutting findings dialog for a review run.
   *
   * Preconditions: `record.reviewSummary` carries a non-empty `systemic_findings`
   * (the "N systemic pattern(s)" chip is only rendered once it does).
   * Postconditions: opens `CodeReviewSystemicFindingsDialogComponent` with the
   * findings already in hand (no fetch — they are persisted on the summary);
   * this method has no return value.
   */
  onViewSystemicFindings(record: PrReviewRecord): void {
    const data: CodeReviewSystemicFindingsDialogData = {
      findings: record.reviewSummary?.systemic_findings ?? [],
    };
    this.dialog.open(CodeReviewSystemicFindingsDialogComponent, {
      data,
      width: '700px',
      maxWidth: '95vw',
    });
  }
}
