import {
  Component,
  EventEmitter,
  Input,
  OnDestroy,
  OnInit,
  Output,
  ViewChild,
  computed,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpErrorResponse } from '@angular/common/http';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatDialog, MatDialogModule } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatMenuModule } from '@angular/material/menu';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSelectModule } from '@angular/material/select';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import { Subscription, interval } from 'rxjs';
import { AgentCatalogApiService } from '../../../services/agent-catalog-api.service';
import { AgentRunnerApiService } from '../../../services/agent-runner-api.service';
import type {
  AgentDetail,
  AgentSummary,
} from '../../../models/agent-catalog.model';
import type {
  InvokeEnvelope,
  SandboxHandle,
  SandboxStatus,
} from '../../../models/agent-runner.model';
import type {
  RunSummary,
  SavedInput,
} from '../../../models/agent-history.model';
import { AgentRunHistoryComponent } from '../agent-run-history/agent-run-history.component';
import { AgentSchemaFormComponent } from '../agent-schema-form/agent-schema-form.component';
import {
  AgentDiffDialogComponent,
  type AgentDiffDialogData,
} from '../agent-diff-dialog/agent-diff-dialog.component';
import {
  SaveInputDialogComponent,
  type SaveInputDialogData,
  type SaveInputDialogResult,
} from '../save-input-dialog/save-input-dialog.component';

/**
 * Runner tab for the Agent Console.
 *
 * Phase 2: picks an agent, loads a sample, warms a sandbox, invokes, shows
 * the envelope.
 *
 * Phase 3: golden + saved inputs in the picker, optional Form editor,
 * persistent run history with click-to-reload, and a Compare dialog that
 * produces a unified diff.
 */
@Component({
  selector: 'app-agent-runner',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatButtonModule,
    MatButtonToggleModule,
    MatCardModule,
    MatChipsModule,
    MatDialogModule,
    MatFormFieldModule,
    MatIconModule,
    MatInputModule,
    MatMenuModule,
    MatProgressSpinnerModule,
    MatSelectModule,
    MatTooltipModule,
    AgentRunHistoryComponent,
    AgentSchemaFormComponent,
  ],
  templateUrl: './agent-runner.component.html',
  styleUrl: './agent-runner.component.scss',
})
export class AgentRunnerComponent implements OnInit, OnDestroy {
  private readonly catalog = inject(AgentCatalogApiService);
  private readonly runner = inject(AgentRunnerApiService);
  private readonly dialog = inject(MatDialog);

  /** Preselect an agent (wired from the Catalog drawer). */
  @Input() set preselectedAgentId(value: string | null) {
    if (value && value !== this.selectedAgentId()) {
      this.selectedAgentId.set(value);
      this.loadAgentDetail(value);
    }
  }

  @Output() readonly requestCatalogReturn = new EventEmitter<void>();

  @ViewChild(AgentRunHistoryComponent) historyPanel?: AgentRunHistoryComponent;

  readonly agents = signal<AgentSummary[]>([]);
  readonly selectedAgentId = signal<string | null>(null);
  readonly selectedAgent = signal<AgentDetail | null>(null);

  readonly goldenSamples = signal<string[]>([]);
  readonly savedInputs = signal<SavedInput[]>([]);
  /** ``null`` for ad-hoc; `"golden:<name>"` or `"saved:<id>"` otherwise. */
  readonly selectedPickerValue = signal<string | null>(null);

  readonly inputText = signal<string>('{}');
  readonly inputError = signal<string | null>(null);
  readonly inputSchema = signal<unknown | null>(null);
  readonly editorMode = signal<'form' | 'json'>('json');

  readonly sandbox = signal<SandboxHandle | null>(null);
  readonly sandboxPolling = signal<boolean>(false);

  readonly running = signal<boolean>(false);
  readonly lastResponse = signal<InvokeEnvelope | null>(null);
  readonly lastError = signal<string | null>(null);
  readonly activeRunId = signal<string | null>(null);

  readonly requiresLiveIntegration = computed(() => {
    const detail = this.selectedAgent();
    if (!detail) return false;
    return detail.manifest.tags?.includes('requires-live-integration') ?? false;
  });

  readonly canRun = computed(() => {
    if (!this.selectedAgent()) return false;
    if (this.requiresLiveIntegration()) return false;
    if (this.inputError()) return false;
    if (this.running()) return false;
    return true;
  });

  readonly sandboxStatusLabel = computed<SandboxStatus | 'cold'>(
    () => this.sandbox()?.status ?? 'cold',
  );

  readonly parsedInput = computed<unknown>(() => {
    try {
      return JSON.parse(this.inputText() || '{}');
    } catch {
      return {};
    }
  });

  private sandboxPollSub: Subscription | null = null;

  ngOnInit(): void {
    this.catalog.listAgents().subscribe({
      next: (agents) => this.agents.set(agents),
      error: (err) => console.error('Runner: failed to load agents', err),
    });
  }

  ngOnDestroy(): void {
    this.sandboxPollSub?.unsubscribe();
  }

  // ---------------------------------------------------------------
  // Agent selection
  // ---------------------------------------------------------------

  onAgentChange(id: string | null): void {
    this.selectedAgentId.set(id);
    this.selectedAgent.set(null);
    this.goldenSamples.set([]);
    this.savedInputs.set([]);
    this.selectedPickerValue.set(null);
    this.inputText.set('{}');
    this.inputError.set(null);
    this.inputSchema.set(null);
    this.editorMode.set('json');
    this.lastResponse.set(null);
    this.lastError.set(null);
    this.activeRunId.set(null);
    this.sandbox.set(null);
    this.sandboxPollSub?.unsubscribe();
    this.sandboxPollSub = null;
    if (id) this.loadAgentDetail(id);
  }

  private loadAgentDetail(id: string): void {
    this.catalog.getAgent(id).subscribe({
      next: (detail) => {
        this.selectedAgent.set(detail);
        this.loadSamples(id);
        this.loadSavedInputs(id);
        this.loadInputSchema(id);
        this.startSandboxPolling(detail.manifest.team);
      },
      error: (err) => {
        console.error('Failed to load agent detail', err);
        this.lastError.set('Could not load agent detail.');
      },
    });
  }

  private loadSamples(id: string): void {
    this.runner.listSamples(id).subscribe({
      next: (samples) => {
        this.goldenSamples.set(samples);
        if (samples.length > 0 && !this.selectedPickerValue()) {
          this.applyGoldenSample(samples[0]);
        }
      },
      error: () => this.goldenSamples.set([]),
    });
  }

  private loadSavedInputs(id: string): void {
    this.runner.listSavedInputs(id).subscribe({
      next: (items) => this.savedInputs.set(items),
      error: () => this.savedInputs.set([]),
    });
  }

  private loadInputSchema(id: string): void {
    this.catalog.getInputSchema(id).subscribe({
      next: (schema) => {
        this.inputSchema.set(schema);
        // Default to form when a schema is available. The form component
        // will fall back to raw JSON internally if it can't render.
        this.editorMode.set('form');
      },
      error: () => {
        this.inputSchema.set(null);
        this.editorMode.set('json');
      },
    });
  }

  // ---------------------------------------------------------------
  // Picker handling
  // ---------------------------------------------------------------

  onPickerChange(value: string | null): void {
    if (!value) {
      this.selectedPickerValue.set(null);
      return;
    }
    if (value.startsWith('golden:')) {
      this.applyGoldenSample(value.slice(7));
    } else if (value.startsWith('saved:')) {
      this.applySavedInput(value.slice(6));
    }
  }

  private applyGoldenSample(name: string): void {
    const agent = this.selectedAgentId();
    if (!agent) return;
    this.selectedPickerValue.set(`golden:${name}`);
    this.runner.getSample(agent, name).subscribe({
      next: (body) => this.setInputFromPicker(body),
      error: () => this.inputError.set('Could not load sample.'),
    });
  }

  private applySavedInput(savedId: string): void {
    const match = this.savedInputs().find((s) => s.id === savedId);
    if (!match) return;
    this.selectedPickerValue.set(`saved:${savedId}`);
    this.setInputFromPicker(match.input_data);
  }

  private setInputFromPicker(body: unknown): void {
    this.inputText.set(JSON.stringify(body, null, 2));
    this.inputError.set(null);
  }

  resetInput(): void {
    this.inputText.set('{}');
    this.selectedPickerValue.set(null);
    this.inputError.set(null);
  }

  onInputTextChange(value: string): void {
    this.inputText.set(value);
    try {
      JSON.parse(value || '{}');
      this.inputError.set(null);
    } catch (e) {
      this.inputError.set((e as Error).message);
    }
  }

  onFormValueChange(value: unknown): void {
    const serialised = JSON.stringify(value ?? {}, null, 2);
    this.inputText.set(serialised);
    this.inputError.set(null);
  }

  // ---------------------------------------------------------------
  // Save + delete saved inputs
  // ---------------------------------------------------------------

  openSaveInputDialog(): void {
    const agent = this.selectedAgentId();
    if (!agent) return;
    let body: unknown;
    try {
      body = JSON.parse(this.inputText() || '{}');
    } catch (e) {
      this.inputError.set((e as Error).message);
      return;
    }
    const ref = this.dialog.open<
      SaveInputDialogComponent,
      SaveInputDialogData,
      SaveInputDialogResult
    >(SaveInputDialogComponent, { data: { mode: 'create' } });
    ref.afterClosed().subscribe((result) => {
      if (!result) return;
      this.runner
        .createSavedInput(agent, {
          name: result.name,
          input_data: body,
          description: result.description,
        })
        .subscribe({
          next: (saved) => {
            this.savedInputs.update((rows) => [saved, ...rows]);
            this.selectedPickerValue.set(`saved:${saved.id}`);
          },
          error: (err) => {
            // Mat dialog is already closed; surface via snackbar-equivalent.
            alert(err?.error?.detail ?? err?.message ?? 'Failed to save input');
          },
        });
    });
  }

  deleteSavedInput(savedId: string, event: Event): void {
    event.stopPropagation();
    const match = this.savedInputs().find((s) => s.id === savedId);
    if (!match) return;
    if (!confirm(`Delete saved input "${match.name}"?`)) return;
    this.runner.deleteSavedInput(savedId).subscribe({
      next: () => {
        this.savedInputs.update((rows) => rows.filter((s) => s.id !== savedId));
        if (this.selectedPickerValue() === `saved:${savedId}`) {
          this.selectedPickerValue.set(null);
        }
      },
    });
  }

  // ---------------------------------------------------------------
  // Sandbox lifecycle
  // ---------------------------------------------------------------

  private startSandboxPolling(team: string): void {
    this.sandboxPollSub?.unsubscribe();
    this.runner.getSandbox(team).subscribe({
      next: (handle) => this.sandbox.set(handle),
      error: () => this.sandbox.set(null),
    });
    this.sandboxPollSub = interval(5000).subscribe(() => {
      this.runner.getSandbox(team).subscribe({
        next: (handle) => this.sandbox.set(handle),
      });
    });
  }

  warmSandbox(): void {
    const team = this.selectedAgent()?.manifest.team;
    if (!team) return;
    this.sandboxPolling.set(true);
    this.runner.ensureWarm(team).subscribe({
      next: (handle) => {
        this.sandbox.set(handle);
        this.sandboxPolling.set(false);
      },
      error: (err) => {
        console.error('ensureWarm failed', err);
        this.sandboxPolling.set(false);
      },
    });
  }

  tearDownSandbox(): void {
    const team = this.selectedAgent()?.manifest.team;
    if (!team) return;
    if (!confirm(`Tear down the ${team} sandbox?`)) return;
    this.runner.teardown(team).subscribe({
      next: () => {
        this.sandbox.set({ ...(this.sandbox() as SandboxHandle), status: 'cold', url: null });
      },
      error: (err) => console.error('teardown failed', err),
    });
  }

  // ---------------------------------------------------------------
  // Invoke
  // ---------------------------------------------------------------

  run(): void {
    const id = this.selectedAgentId();
    if (!id) return;
    let body: unknown;
    try {
      body = JSON.parse(this.inputText() || '{}');
    } catch (e) {
      this.inputError.set((e as Error).message);
      return;
    }
    const savedId = this.selectedPickerValue()?.startsWith('saved:')
      ? this.selectedPickerValue()!.slice(6)
      : null;
    this.running.set(true);
    this.lastResponse.set(null);
    this.lastError.set(null);
    this.activeRunId.set(null);
    this.runner.invoke(id, body, savedId).subscribe({
      next: (response) => {
        this.running.set(false);
        // 202 is the sandbox "still warming" signal — HttpClient delivers it
        // through `next` because 202 ∈ 2xx, but the body is the warming
        // envelope `{status, message, sandbox}`, NOT an InvokeEnvelope.
        // Treating it as a success would leave downstream code reading
        // `trace_id` / `logs_tail` off an object that doesn't have them.
        if (response.status === 202) {
          this.lastError.set('Sandbox is still warming — retry in a few seconds.');
          this.historyPanel?.refresh();
          return;
        }
        this.lastResponse.set(response.body as InvokeEnvelope);
        this.historyPanel?.refresh();
      },
      error: (err: HttpErrorResponse) => {
        this.running.set(false);
        if (err.status === 409) {
          this.lastError.set(err.error?.detail ?? 'Agent not runnable in sandbox.');
        } else if (err.status === 422 && err.error?.detail) {
          // The shim wraps user-space exceptions in a 422 with the envelope
          // as `detail`, so we can surface the output + logs inline.
          this.lastResponse.set(err.error.detail as InvokeEnvelope);
        } else {
          this.lastError.set(err.error?.detail ?? err.message ?? 'Invocation failed.');
        }
        this.historyPanel?.refresh();
      },
    });
  }

  // ---------------------------------------------------------------
  // Run history interaction
  // ---------------------------------------------------------------

  onHistoryLoadRun(runId: string): void {
    this.runner.getRun(runId).subscribe({
      next: (record) => {
        this.activeRunId.set(record.id);
        this.inputText.set(JSON.stringify(record.input_data ?? {}, null, 2));
        this.inputError.set(null);
        this.lastResponse.set({
          output: record.output_data,
          duration_ms: record.duration_ms,
          trace_id: record.trace_id,
          logs_tail: record.logs_tail,
          error: record.error,
          sandbox: record.sandbox_url
            ? { team: record.team, url: record.sandbox_url }
            : undefined,
        });
        this.lastError.set(null);
      },
      error: (err) => console.error('Failed to load run', err),
    });
  }

  onHistoryCompare(run: RunSummary): void {
    const agent = this.selectedAgentId();
    if (!agent) return;
    const data: AgentDiffDialogData = {
      agentId: agent,
      initialLeft: { kind: 'run', ref: run.id, side: 'output' },
      initialLeftLabel: `run:${run.trace_id.slice(0, 8)}:output`,
    };
    this.dialog.open(AgentDiffDialogComponent, {
      data,
      width: '800px',
      maxWidth: '95vw',
    });
  }

  compareCurrentOutput(): void {
    const agent = this.selectedAgentId();
    const env = this.lastResponse();
    if (!agent || !env) return;
    const data: AgentDiffDialogData = {
      agentId: agent,
      initialLeft: { kind: 'inline', data: env.output },
      initialLeftLabel: 'left:current-output',
    };
    this.dialog.open(AgentDiffDialogComponent, {
      data,
      width: '800px',
      maxWidth: '95vw',
    });
  }

  returnToCatalog(): void {
    this.requestCatalogReturn.emit();
  }

  // ---------------------------------------------------------------
  // View helpers
  // ---------------------------------------------------------------

  prettyOutput(): string {
    const env = this.lastResponse();
    if (!env) return '';
    return JSON.stringify(env.output, null, 2);
  }

  prettySchema(): string {
    const s = this.inputSchema();
    return s ? JSON.stringify(s, null, 2) : '';
  }

  trackAgent(_i: number, a: AgentSummary): string {
    return a.id;
  }
}
