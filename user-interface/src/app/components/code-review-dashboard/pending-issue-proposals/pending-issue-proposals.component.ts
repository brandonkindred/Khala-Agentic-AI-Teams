import {
  Component,
  EventEmitter,
  Input,
  Output,
  ChangeDetectionStrategy,
  OnChanges,
  SimpleChanges,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import type {
  PendingIssueProposal,
  PendingIssueProposalLocation,
} from '../../../models/coding-team.model';
import { InlineBannerComponent } from '../../../shared/inline-banner/inline-banner.component';

@Component({
  selector: 'app-pending-issue-proposals',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, MatIconModule, MatButtonModule, MatProgressSpinnerModule, InlineBannerComponent],
  templateUrl: './pending-issue-proposals.component.html',
  styleUrl: './pending-issue-proposals.component.scss',
})
/**
 * Presentational block of pre-existing-bug issue proposals for one review run.
 * Owns its own selection state (`selectedProposalIds`) and emits user intent to
 * the parent `CodeReviewDashboardComponent` via `createIssuesRequested`; it never
 * calls the GitHub API or mutates its inputs itself. OnPush — the parent feeds
 * it a fresh `proposals` reference once issues are filed.
 */
export class PendingIssueProposalsComponent implements OnChanges {
  /** Pre-existing-bug proposals for the review run this instance renders. */
  @Input({ required: true }) proposals!: PendingIssueProposal[];
  /** True while this review's "create issues" request is in flight. */
  @Input() creatingIssues = false;
  /** This review's last "create issues" failure, if any. */
  @Input() createIssueError: string | null = null;

  /** Emitted with the selected proposal ids when "Create GitHub issue(s)" is clicked. */
  @Output() createIssuesRequested = new EventEmitter<string[]>();

  private selectedProposalIds = new Set<string>();

  ngOnChanges(changes: SimpleChanges): void {
    // The parent replaces `proposals` with the server's updated copy after filing
    // issues (filed proposals now carry `issue_url`). Prune the local selection to
    // whatever is still open in that fresh list — not just the ones this instance
    // filed — so a proposal skipped server-side (already filed by another tab, or
    // an unknown id) never lingers selected.
    if (changes['proposals'] && !changes['proposals'].firstChange) {
      const openIds = new Set(this.openProposals.map((p) => p.id));
      this.selectedProposalIds = new Set(
        Array.from(this.selectedProposalIds).filter((id) => openIds.has(id)),
      );
    }
  }

  /** Proposals not yet filed as a GitHub issue (selectable). */
  get openProposals(): PendingIssueProposal[] {
    return this.proposals.filter((p) => !p.issue_url);
  }

  /**
   * A proposal's location summary for display: `path:line` (or `path`, or '')
   * for a single-location proposal, or `"{N} locations"` when the reviewer
   * combined several similar findings into one proposal.
   */
  proposalLocation(proposal: PendingIssueProposal): string {
    if ((proposal.locations?.length ?? 0) > 1) {
      return `${proposal.locations!.length} locations`;
    }
    return this.formatLocation(proposal.file_path, proposal.line);
  }

  /** True when a proposal combines more than one finding's location. */
  isCombinedProposal(proposal: PendingIssueProposal): boolean {
    return (proposal.locations?.length ?? 0) > 1;
  }

  /** A single location's `path:line` (or `path`) text for display. */
  locationText(location: PendingIssueProposalLocation): string {
    return this.formatLocation(location.file_path, location.line);
  }

  private formatLocation(filePath: string, line: number | null): string {
    if (!filePath) return '';
    return line ? `${filePath}:${line}` : filePath;
  }

  /** Whether a proposal is currently selected for filing. */
  isProposalSelected(proposalId: string): boolean {
    return this.selectedProposalIds.has(proposalId);
  }

  /** Toggle a proposal's selection. */
  toggleProposal(proposalId: string): void {
    if (this.selectedProposalIds.has(proposalId)) {
      this.selectedProposalIds.delete(proposalId);
    } else {
      this.selectedProposalIds.add(proposalId);
    }
  }

  /** Number of proposals selected for filing. */
  get selectedCount(): number {
    return this.selectedProposalIds.size;
  }

  /** Request that the parent file the currently-selected proposals as GitHub issues. */
  requestCreateIssues(): void {
    if (this.selectedProposalIds.size === 0 || this.creatingIssues) return;
    this.createIssuesRequested.emit(Array.from(this.selectedProposalIds));
  }
}
