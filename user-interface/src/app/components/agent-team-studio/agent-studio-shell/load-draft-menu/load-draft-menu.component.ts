import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatMenuModule } from '@angular/material/menu';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { extractErrorDetail } from '../../../../core/error-handler.interceptor';
import { AgentStudioApiService } from '../../../../services/agent-studio-api.service';
import type { AgentStudioDraftSummary } from '../../../../models/agent-studio.model';

/** Number of draft summaries fetched per page (spec §3.5: "show older" via offset). */
const PAGE_SIZE = 10;

/**
 * Load-draft dropdown (spec §3.5). Lists saved-draft summaries, paginated via
 * a trailing "Show older" control, and emits `draftSelected` when a row is
 * picked — hydration itself is the shell's responsibility (it needs
 * `AgentStudioStateService` and a second API call this menu has no reason to
 * know about).
 */
@Component({
  selector: 'app-load-draft-menu',
  standalone: true,
  imports: [DatePipe, MatButtonModule, MatIconModule, MatMenuModule, MatProgressSpinnerModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './load-draft-menu.component.html',
  styleUrl: './load-draft-menu.component.scss',
})
export class LoadDraftMenuComponent {
  private readonly api = inject(AgentStudioApiService);

  /** Disables the trigger while the shell is mid-hydration from a prior selection. */
  @Input() busy = false;

  /** Emits the selected draft's id; this component performs no hydration itself. */
  @Output() readonly draftSelected = new EventEmitter<string>();

  readonly drafts = signal<AgentStudioDraftSummary[]>([]);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly hasMore = signal(false);
  private nextOffset = 0;
  /** Bumped on every `onOpened()`; lets a fetch from a since-reopened menu
   *  recognize its response is stale and discard it instead of appending
   *  duplicate rows / double-advancing the offset. */
  private openToken = 0;

  /**
   * Wired to `<mat-menu (opened)>`. Always refetches page 1 rather than
   * reusing a stale list, so a draft saved since the menu was last opened
   * (or deleted, once #5914 lands) shows up correctly.
   *
   * Preconditions: none.
   * Postconditions: `drafts()`/`hasMore()` reflect the first page; `loading()`
   *   is `true` until the request settles. Any still-in-flight fetch from a
   *   previous opening is superseded and its response discarded on arrival.
   */
  onOpened(): void {
    const token = ++this.openToken;
    this.nextOffset = 0;
    this.drafts.set([]);
    this.hasMore.set(false);
    this.fetchPage(token);
  }

  /**
   * Fetch the next page and append it to the list.
   *
   * Preconditions: none — a no-op while a fetch is already in flight or no
   *   further pages exist, rather than the caller having to check first.
   * Postconditions: when it ran, `drafts()` grows by the fetched rows and
   *   `hasMore()`/the offset are updated for a subsequent call.
   */
  loadMore(): void {
    if (this.loading() || !this.hasMore()) return;
    this.fetchPage(this.openToken);
  }

  /**
   * Select a draft row.
   *
   * Preconditions: `draftId` is a non-empty id from a rendered row.
   * Postconditions: `draftSelected` emits exactly once with `draftId`; no
   *   HTTP call is made by this component.
   */
  select(draftId: string): void {
    this.draftSelected.emit(draftId);
  }

  private fetchPage(token: number): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.listDrafts(PAGE_SIZE, this.nextOffset).subscribe({
      next: (rows) => {
        if (token !== this.openToken) return;
        this.drafts.update((existing) => [...existing, ...rows]);
        this.hasMore.set(rows.length >= PAGE_SIZE);
        this.nextOffset += rows.length;
        this.loading.set(false);
      },
      error: (err) => {
        if (token !== this.openToken) return;
        this.loading.set(false);
        this.error.set(extractErrorDetail(err, 'Failed to load drafts.'));
      },
    });
  }
}
