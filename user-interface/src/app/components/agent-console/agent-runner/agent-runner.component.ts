import {
  Component,
  EventEmitter,
  Input,
  OnDestroy,
  OnInit,
  Output,
  computed,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSelectModule } from '@angular/material/select';
import { MatTooltipModule } from '@angular/material/tooltip';
import { HttpErrorResponse } from '@angular/common/http';
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

/**
 * Runner tab for the Agent Console.
 *
 * Lets a user pick an agent, load a golden sample (or type raw JSON), warm a
 * per-team Docker sandbox, invoke the agent, and inspect the output/logs/trace.
 */
@Component({
  selector: 'app-agent-runner',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatButtonModule,
    MatCardModule,
    MatChipsModule,
    MatFormFieldModule,
    MatIconModule,
    MatInputModule,
    MatProgressSpinnerModule,
    MatSelectModule,
    MatTooltipModule,
  ],
  templateUrl: './agent-runner.component.html',
  styleUrl: './agent-runner.component.scss',
})
export class AgentRunnerComponent implements OnInit, OnDestroy {
  private readonly catalog = inject(AgentCatalogApiService);
  private readonly runner = inject(AgentRunnerApiService);

  /** Preselect an agent (wired from the Catalog drawer). */
  @Input() set preselectedAgentId(value: string | null) {
    if (value && value !== this.selectedAgentId()) {
      this.selectedAgentId.set(value);
      this.loadAgentDetail(value);
    }
  }

  @Output() readonly requestCatalogReturn = new EventEmitter<void>();

  readonly agents = signal<AgentSummary[]>([]);
  readonly selectedAgentId = signal<string | null>(null);
  readonly selectedAgent = signal<AgentDetail | null>(null);

  readonly samples = signal<string[]>([]);
  readonly selectedSample = signal<string | null>(null);

  readonly inputText = signal<string>('{}');
  readonly inputError = signal<string | null>(null);
  readonly inputSchema = signal<unknown | null>(null);

  readonly sandbox = signal<SandboxHandle | null>(null);
  readonly sandboxPolling = signal<boolean>(false);

  readonly running = signal<boolean>(false);
  readonly lastResponse = signal<InvokeEnvelope | null>(null);
  readonly lastError = signal<string | null>(null);

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
    this.samples.set([]);
    this.selectedSample.set(null);
    this.inputText.set('{}');
    this.inputError.set(null);
    this.inputSchema.set(null);
    this.lastResponse.set(null);
    this.lastError.set(null);
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
        this.samples.set(samples);
        if (samples.length > 0) this.applySample(samples[0]);
      },
      error: () => this.samples.set([]),
    });
  }

  private loadInputSchema(id: string): void {
    this.catalog.getInputSchema(id).subscribe({
      next: (schema) => this.inputSchema.set(schema),
      error: () => this.inputSchema.set(null),
    });
  }

  // ---------------------------------------------------------------
  // Sample handling
  // ---------------------------------------------------------------

  applySample(name: string): void {
    const agent = this.selectedAgentId();
    if (!agent) return;
    this.selectedSample.set(name);
    this.runner.getSample(agent, name).subscribe({
      next: (body) => {
        this.inputText.set(JSON.stringify(body, null, 2));
        this.inputError.set(null);
      },
      error: (err) => {
        console.error('Failed to load sample', err);
        this.inputError.set('Could not load sample.');
      },
    });
  }

  resetInput(): void {
    this.inputText.set('{}');
    this.selectedSample.set(null);
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
    this.running.set(true);
    this.lastResponse.set(null);
    this.lastError.set(null);
    this.runner.invoke(id, body).subscribe({
      next: (envelope) => {
        this.lastResponse.set(envelope);
        this.running.set(false);
      },
      error: (err: HttpErrorResponse) => {
        this.running.set(false);
        if (err.status === 202) {
          this.lastError.set('Sandbox is still warming — retry in a few seconds.');
        } else if (err.status === 409) {
          this.lastError.set(err.error?.detail ?? 'Agent not runnable in sandbox.');
        } else if (err.status === 422 && err.error?.detail) {
          // shim wraps user-space exceptions in a 422 with the envelope as detail.
          this.lastResponse.set(err.error.detail as InvokeEnvelope);
        } else {
          this.lastError.set(err.error?.detail ?? err.message ?? 'Invocation failed.');
        }
      },
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
