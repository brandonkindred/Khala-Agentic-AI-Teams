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
import { MatCheckboxModule } from '@angular/material/checkbox';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTooltipModule } from '@angular/material/tooltip';
import type { OutOfScopeProposalItem } from '../../../models/integrations.model';
import { InlineBannerComponent } from '../../../shared/inline-banner/inline-banner.component';

/**
 * Displays all unfiled out-of-scope issue proposals for a repository.
 * Users can select individual proposals or all at once, then file them
 * as enhanced GitHub issues via the "Add Github Issues" action.
 *
 * Presentational: the parent owns loading/filing state and the API calls.
 * This component manages selection state and emits user intent.
 */
@Component({
  selector: 'app-out-of-scope-issues',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    CommonModule,
    MatIconModule,
    MatButtonModule,
    MatCheckboxModule,
    MatProgressSpinnerModule,
    MatTooltipModule,
    InlineBannerComponent,
  ],
  templateUrl: './out-of-scope-issues.component.html',
  styleUrl: './out-of-scope-issues.component.scss',
})
export class OutOfScopeIssuesComponent implements OnChanges {
  /** All unfiled out-of-scope proposals to display. */
  @Input({ required: true }) proposals: OutOfScopeProposalItem[] = [];

  /** Whether proposals are currently being loaded. */
  @Input() loading = false;

  /** Whether a filing request is currently in flight. */
  @Input() filing = false;

  /** Error message from loading or filing, if any. */
  @Input() error: string | null = null;

  /** Emitted when the user clicks "Add Github Issues" with the selected composite ids. */
  @Output() fileIssuesRequested = new EventEmitter<string[]>();

  /** Emitted when the user wants to refresh the list. */
  @Output() refreshRequested = new EventEmitter<void>();

  /** Set of selected composite proposal ids (job_id:proposal_id). */
  private selectedIds = new Set<string>();

  ngOnChanges(changes: SimpleChanges): void {
    // When the proposals list changes (e.g., after filing removes items),
    // prune the selection to only IDs still present in the new list.
    if (changes['proposals'] && !changes['proposals'].firstChange) {
      const presentIds = new Set(this.proposals.map((p) => this.compositeId(p)));
      this.selectedIds = new Set(
        Array.from(this.selectedIds).filter((id) => presentIds.has(id)),
      );
    }
  }

  /** Composite id for a proposal (used as selection key and sent to backend). */
  compositeId(proposal: OutOfScopeProposalItem): string {
    return `${proposal.job_id}:${proposal.id}`;
  }

  /** Whether a proposal is currently selected. */
  isSelected(proposal: OutOfScopeProposalItem): boolean {
    return this.selectedIds.has(this.compositeId(proposal));
  }

  /** Toggle a single proposal's selection. */
  toggleSelection(proposal: OutOfScopeProposalItem): void {
    const id = this.compositeId(proposal);
    if (this.selectedIds.has(id)) {
      this.selectedIds.delete(id);
    } else {
      this.selectedIds.add(id);
    }
  }

  /** Whether all proposals are currently selected. */
  get allSelected(): boolean {
    return this.proposals.length > 0 && this.selectedIds.size === this.proposals.length;
  }

  /** Whether some (but not all) proposals are selected (for indeterminate checkbox state). */
  get someSelected(): boolean {
    return this.selectedIds.size > 0 && this.selectedIds.size < this.proposals.length;
  }

  /** Number of selected proposals. */
  get selectedCount(): number {
    return this.selectedIds.size;
  }

  /** Select all proposals. */
  selectAll(): void {
    for (const p of this.proposals) {
      this.selectedIds.add(this.compositeId(p));
    }
  }

  /** Deselect all proposals. */
  deselectAll(): void {
    this.selectedIds.clear();
  }

  /** Toggle between select-all and deselect-all. */
  toggleSelectAll(): void {
    if (this.allSelected) {
      this.deselectAll();
    } else {
      this.selectAll();
    }
  }

  /** Location summary for display. */
  proposalLocation(proposal: OutOfScopeProposalItem): string {
    if ((proposal.locations?.length ?? 0) > 1) {
      return `${proposal.locations.length} locations`;
    }
    if (!proposal.file_path) return '';
    return proposal.line ? `${proposal.file_path}:${proposal.line}` : proposal.file_path;
  }

  /** Request filing of the selected proposals. */
  onFileIssues(): void {
    if (this.selectedIds.size === 0 || this.filing) return;
    this.fileIssuesRequested.emit(Array.from(this.selectedIds));
  }

  /** Request a refresh of the proposals list. */
  onRefresh(): void {
    this.refreshRequested.emit();
  }

  /** After successful filing, prune the selection to only still-present proposals. */
  pruneSelection(): void {
    const presentIds = new Set(this.proposals.map((p) => this.compositeId(p)));
    this.selectedIds = new Set(
      Array.from(this.selectedIds).filter((id) => presentIds.has(id)),
    );
  }
}
