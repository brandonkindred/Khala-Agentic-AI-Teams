import { Component, DestroyRef, OnInit, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Observable } from 'rxjs';
import { FormsModule } from '@angular/forms';
import { CdkDragDrop, DragDropModule, moveItemInArray } from '@angular/cdk/drag-drop';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatSelectModule } from '@angular/material/select';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatCheckboxModule } from '@angular/material/checkbox';
import { LlmConfigApiService } from '../../services/llm-config-api.service';
import type {
  LlmProvider,
  LlmProviderCreate,
  LlmProviderEntry,
  LlmProviderListResponse,
  LlmProviderUpdate,
  LlmStorageStatus,
} from '../../models/llm-config.model';

const OLLAMA_LOCAL_DEFAULT = 'http://localhost:11434';

/** Editable form fields for a provider list entry (add or edit). */
interface ProviderForm {
  label: string;
  provider: LlmProvider;
  model: string;
  base_url: string;
  api_key: string;
  /** Edit only: remove the stored key (ignored when a new api_key is typed). */
  clear_api_key: boolean;
}

function emptyProviderForm(): ProviderForm {
  return { label: '', provider: 'ollama', model: '', base_url: '', api_key: '', clear_api_key: false };
}

/**
 * LLM Provider settings page. Manages the ordered multi-provider fallback list —
 * the sole source of LLM configuration. Each entry carries its own provider, model,
 * base URL, and API key; agents use the most-preferred non-usage-limited provider
 * and fall back to the next on a 429. API keys are write-only: the server returns
 * `api_key_configured` booleans, so inputs are never pre-filled.
 */
@Component({
  selector: 'app-llm-config-dashboard',
  standalone: true,
  imports: [
    FormsModule,
    DragDropModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatSelectModule,
    MatIconModule,
    MatProgressSpinnerModule,
    MatTooltipModule,
    MatCheckboxModule,
  ],
  templateUrl: './llm-config-dashboard.component.html',
  styleUrl: './llm-config-dashboard.component.scss',
})
export class LlmConfigDashboardComponent implements OnInit {
  private readonly api = inject(LlmConfigApiService);
  private readonly destroyRef = inject(DestroyRef);

  /** Ordered providers (most→least preferred). */
  providers: LlmProviderEntry[] = [];
  providersLoading = false;
  providersError: string | null = null;
  providersSaving = false;
  success: string | null = null;
  /** The add-provider form is shown when this is non-null. */
  addForm: ProviderForm | null = null;
  /** The id of the entry being edited inline, or null. */
  editingId: number | null = null;
  editForm: ProviderForm = emptyProviderForm();

  // Why the store is (un)writable, so the banner can say *why* mutations are
  // disabled rather than always blaming "not configured". `storageAvailable`
  // derives from it so the two can never drift.
  storageStatus: LlmStorageStatus = 'available';

  /** Adding/editing is allowed only when the store is configured AND reachable. */
  get storageAvailable(): boolean {
    return this.storageStatus === 'available';
  }

  /** Clock tick for the reset-time badges (`resetInfo`); refreshed every 30s so a
   * limited entry's "resets in ~Xm" stays live without the operator interacting. */
  now = Date.now();

  ngOnInit(): void {
    this.loadProviders();
    // Skip the tick (and the change-detection pass it triggers) when nothing is
    // currently limited — resetInfo's output can't change if no badge is showing.
    const timer = setInterval(() => {
      if (this.providers.some((p) => p.limit_exceeded)) {
        this.now = Date.now();
      }
    }, 30_000);
    this.destroyRef.onDestroy(() => clearInterval(timer));
  }

  /** Load the ordered provider list. */
  loadProviders(): void {
    this.providersLoading = true;
    this.providersError = null;
    this.api
      .listProviders()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (res) => {
          this.applyProviderList(res);
          this.providersLoading = false;
        },
        error: (err) => {
          this.providersError = this.friendlyError(err, 'Failed to load provider list.');
          this.providersLoading = false;
        },
      });
  }

  private applyProviderList(res: LlmProviderListResponse): void {
    // Trust the server order; sort defensively in case a client reorders the array.
    this.providers = [...(res.providers ?? [])].sort((a, b) => a.sort_order - b.sort_order);
    if (res.storage_status) {
      this.storageStatus = res.storage_status;
    }
  }

  /** Reorder the list on drag-drop and persist the new order. */
  onProviderDrop(event: CdkDragDrop<LlmProviderEntry[]>): void {
    // Ignore a drop while a save is already in flight: two concurrent reorder
    // requests could resolve out of order and corrupt the persisted order. The drop
    // list is also disabled in the template while saving (belt-and-suspenders).
    if (this.providersSaving) {
      return;
    }
    if (event.previousIndex === event.currentIndex) {
      return;
    }
    // Reorder is the only optimistic update — snapshot the previous order so the
    // error path can revert it (the list is updated locally before the server confirms).
    const previous = [...this.providers];
    moveItemInArray(this.providers, event.previousIndex, event.currentIndex);
    const ids = this.providers.map((p) => p.id);
    this.persistProviders(this.api.reorderProviders(ids), 'Provider order saved.', {
      revert: () => {
        this.providers = previous;
      },
    });
  }

  /** Open the add-provider form with sensible defaults. */
  startAdd(): void {
    // Opening a new form while another mutation is in flight would clear
    // providersError now, only for that mutation's error handler to set it again
    // later — attributed to a form the operator has already moved on from. The
    // trigger button is also disabled in the template while saving (belt-and-suspenders).
    if (this.providersSaving) {
      return;
    }
    this.editingId = null;
    this.addForm = { ...emptyProviderForm(), base_url: OLLAMA_LOCAL_DEFAULT };
    this.providersError = null;
  }

  cancelAdd(): void {
    this.addForm = null;
    this.providersError = null;
  }

  /** Submit the add-provider form. */
  submitAdd(): void {
    if (!this.addForm) {
      return;
    }
    const form = this.addForm;
    if (!form.label.trim()) {
      this.providersError = 'Please enter a label for the provider.';
      return;
    }
    const body: LlmProviderCreate = {
      label: form.label.trim(),
      provider: form.provider,
      model: form.model.trim(),
      base_url: form.provider === 'ollama' ? form.base_url.trim() : '',
      api_key: form.api_key.trim(),
    };
    this.persistProviders(this.api.createProvider(body), 'Provider added.', {
      onSuccess: () => {
        this.addForm = null;
      },
    });
  }

  /** Begin inline editing of an entry (keys are never pre-filled). */
  startEdit(entry: LlmProviderEntry): void {
    // Same race as startAdd: don't switch context (and clear providersError) while
    // another entry's mutation is still in flight. See startAdd for the scenario.
    if (this.providersSaving) {
      return;
    }
    this.addForm = null;
    this.editingId = entry.id;
    this.editForm = {
      label: entry.label,
      provider: entry.provider,
      model: entry.model,
      base_url: entry.base_url,
      api_key: '',
      clear_api_key: false,
    };
    this.providersError = null;
  }

  cancelEdit(): void {
    this.editingId = null;
    this.providersError = null;
  }

  /**
   * Persist an inline edit. An empty api_key leaves the stored key untouched; the
   * "clear stored key" toggle removes it (ignored when a new key is typed, mirroring
   * the server, which lets a provided key win over the flag).
   */
  submitEdit(): void {
    if (this.editingId === null) {
      return;
    }
    const form = this.editForm;
    if (!form.label.trim()) {
      this.providersError = 'Please enter a label for the provider.';
      return;
    }
    const newKey = form.api_key.trim();
    const body: LlmProviderUpdate = {
      label: form.label.trim(),
      provider: form.provider,
      model: form.model.trim(),
      base_url: form.provider === 'ollama' ? form.base_url.trim() : '',
      api_key: newKey,
      clear_api_key: form.clear_api_key && !newKey,
    };
    this.persistProviders(this.api.updateProvider(this.editingId, body), 'Provider updated.', {
      onSuccess: () => {
        this.editingId = null;
      },
    });
  }

  /** Remove a provider from the list. */
  removeProvider(entry: LlmProviderEntry): void {
    this.persistProviders(this.api.deleteProvider(entry.id), 'Provider removed.');
  }

  /** Run a list-mutating call and surface success/error.
   *
   * The mutating calls apply the server's authoritative list on success
   * (`applyProviderList`); only reorder updates the local array optimistically, so on
   * error the caller's `revert` restores it. We deliberately do NOT reload on error —
   * a reload would clear/overwrite the just-set error message (its HTTP response races
   * the error), and add/edit/delete never touched the local list, so there is nothing
   * to resync.
   */
  private persistProviders(
    call: Observable<LlmProviderListResponse>,
    successMsg: string,
    opts?: { onSuccess?: () => void; revert?: () => void },
  ): void {
    this.providersError = null;
    this.providersSaving = true;
    this.success = null;
    call.pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res) => {
        this.applyProviderList(res);
        this.providersSaving = false;
        this.success = successMsg;
        opts?.onSuccess?.();
      },
      error: (err) => {
        this.providersSaving = false;
        opts?.revert?.();
        this.providersError = this.friendlyError(err, 'Failed to save the provider list.');
      },
    });
  }

  /** Human-readable reset estimate for a usage-limited provider (e.g. "~2h").
   *
   * ``nowMs`` defaults to the current time; tests pass a fixed value for
   * deterministic assertions (no private-field override needed).
   */
  resetInfo(entry: LlmProviderEntry, nowMs: number = Date.now()): string {
    if (!entry.limit_exceeded || !entry.reset_at) {
      return '';
    }
    const reset = new Date(entry.reset_at).getTime();
    if (Number.isNaN(reset)) {
      return '';
    }
    const ms = reset - nowMs;
    if (ms <= 0) {
      return 'resetting now';
    }
    const minutes = Math.round(ms / 60000);
    if (minutes < 1) {
      // Under ~30s rounds to 0 minutes — avoid the confusing "resets in ~0m".
      return 'resets in <1m';
    }
    if (minutes < 60) {
      return `resets in ~${minutes}m`;
    }
    const hours = Math.round(minutes / 60);
    if (hours < 48) {
      return `resets in ~${hours}h`;
    }
    return `resets in ~${Math.round(hours / 24)}d`;
  }

  /** Extract a human-readable error detail from an API error response, falling back to a default message. */
  private friendlyError(err: unknown, fallback: string): string {
    const detail = (err as { error?: { detail?: string } })?.error?.detail;
    return detail || fallback;
  }
}
