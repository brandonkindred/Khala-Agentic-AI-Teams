import { ChangeDetectionStrategy, Component, DestroyRef, OnInit, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import {
  MAT_DIALOG_DATA,
  MatDialogModule,
  MatDialogRef,
} from '@angular/material/dialog';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { Subject, catchError, debounceTime, of, switchMap } from 'rxjs';
import { AgentCatalogApiService } from '../../services/agent-catalog-api.service';
import type { AgentSummary } from '../../models/agent-catalog.model';

/** Debounce (ms) between the last keystroke and the catalog search request. */
export const SEARCH_DEBOUNCE_MS = 300;

export interface AddAgentFromRegistryDialogData {
  /** Manifest ids already on this team's roster, so they render as "Added". */
  existingManifestIds: string[];
}

/** Dialog result: the chosen registry manifest id, or `undefined` on cancel. */
export type AddAgentFromRegistryDialogResult = string;

/**
 * Agent Studio — Stage 3 "+ Add" → "Search registry agents" (spec §3, Stage 3).
 *
 * A minimal search-and-pick dialog over the Agent Console catalog
 * (`AgentCatalogApiService.listAgents`). Selecting a result closes the dialog
 * with the manifest id; the caller (the roster panel) does the actual
 * `POST .../agents/from-registry` and roster refresh.
 */
@Component({
  selector: 'app-add-agent-from-registry-dialog',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatDialogModule,
    MatButtonModule,
    MatFormFieldModule,
    MatIconModule,
    MatInputModule,
    MatProgressSpinnerModule,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './add-agent-from-registry-dialog.component.html',
  styleUrl: './add-agent-from-registry-dialog.component.scss',
})
export class AddAgentFromRegistryDialogComponent implements OnInit {
  private readonly api = inject(AgentCatalogApiService);
  private readonly destroyRef = inject(DestroyRef);
  readonly data = inject<AddAgentFromRegistryDialogData>(MAT_DIALOG_DATA);
  readonly ref =
    inject<MatDialogRef<AddAgentFromRegistryDialogComponent, AddAgentFromRegistryDialogResult>>(
      MatDialogRef,
    );

  readonly query = signal('');
  readonly results = signal<AgentSummary[]>([]);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);

  /**
   * Search pipeline. `debounceTime` collapses a burst of keystrokes into a single
   * request instead of one per character, and `switchMap` cancels the prior
   * in-flight `listAgents` request so a slow earlier response can't land after
   * (and clobber) a newer query's results — the out-of-order race a plain
   * per-call `.subscribe` is prone to.
   */
  private readonly searchInput = new Subject<void>();

  constructor() {
    this.searchInput
      .pipe(
        debounceTime(SEARCH_DEBOUNCE_MS),
        switchMap(() => {
          const q = this.query().trim();
          return this.api.listAgents(q ? { q } : {}).pipe(
            catchError(() => {
              this.loading.set(false);
              this.error.set('Could not search the agent catalog.');
              return of<AgentSummary[] | null>(null);
            }),
          );
        }),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe((agents) => {
        // `null` is the error sentinel from `catchError` — leave the prior
        // results (and the error banner) in place rather than blanking the list.
        if (agents === null) return;
        this.loading.set(false);
        this.results.set(agents);
      });
  }

  ngOnInit(): void {
    this.search();
  }

  onQueryChange(value: string): void {
    this.query.set(value);
    this.search();
  }

  /**
   * Kick off a (debounced, cancellation-safe) catalog search for the current query.
   * The loading flag and error-clear are set here — synchronously, before the
   * debounce — so the dialog shows the spinner immediately (and drops a stale
   * error banner) rather than briefly rendering the "no matches" empty state
   * during the debounce window before the first request even fires.
   */
  search(): void {
    this.loading.set(true);
    this.error.set(null);
    this.searchInput.next();
  }

  isAlreadyOnRoster(agentId: string): boolean {
    return this.data.existingManifestIds.includes(agentId);
  }

  choose(agent: AgentSummary): void {
    if (this.isAlreadyOnRoster(agent.id)) {
      return;
    }
    this.ref.close(agent.id);
  }

  cancel(): void {
    this.ref.close();
  }
}
