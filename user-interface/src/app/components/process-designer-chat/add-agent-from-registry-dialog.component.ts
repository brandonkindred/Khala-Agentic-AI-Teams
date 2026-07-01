import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
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
import { AgentCatalogApiService } from '../../services/agent-catalog-api.service';
import type { AgentSummary } from '../../models/agent-catalog.model';

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
  readonly data = inject<AddAgentFromRegistryDialogData>(MAT_DIALOG_DATA);
  readonly ref =
    inject<MatDialogRef<AddAgentFromRegistryDialogComponent, AddAgentFromRegistryDialogResult>>(
      MatDialogRef,
    );

  readonly query = signal('');
  readonly results = signal<AgentSummary[]>([]);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);

  ngOnInit(): void {
    this.search();
  }

  onQueryChange(value: string): void {
    this.query.set(value);
    this.search();
  }

  search(): void {
    this.loading.set(true);
    this.error.set(null);
    const q = this.query().trim();
    this.api.listAgents(q ? { q } : {}).subscribe({
      next: (agents) => {
        this.loading.set(false);
        this.results.set(agents);
      },
      error: () => {
        this.loading.set(false);
        this.error.set('Could not search the agent catalog.');
      },
    });
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
