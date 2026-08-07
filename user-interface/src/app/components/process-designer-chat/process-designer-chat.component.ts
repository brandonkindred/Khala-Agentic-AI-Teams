import {
  Component,
  EventEmitter,
  Input,
  OnInit,
  OnChanges,
  Output,
  SimpleChanges,
  ViewChild,
  ElementRef,
  AfterViewChecked,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormBuilder, FormsModule, ReactiveFormsModule, Validators } from '@angular/forms';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatChipsModule } from '@angular/material/chips';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatMenuModule } from '@angular/material/menu';
import { MatDialog, MatDialogModule } from '@angular/material/dialog';
import { AgenticTeamApiService } from '../../services/agentic-team-api.service';
import { FlowStepEditorComponent } from '../flow-step-editor/flow-step-editor.component';
import {
  AddAgentFromRegistryDialogComponent,
  type AddAgentFromRegistryDialogData,
  type AddAgentFromRegistryDialogResult,
} from './add-agent-from-registry-dialog.component';
import {
  ConfirmDialogComponent,
  type ConfirmDialogData,
} from '../../shared/confirm-dialog/confirm-dialog.component';
import type {
  AgenticTeam,
  AgenticTeamAgent,
  AgenticConversationMessage,
  ProcessDefinition,
  ProcessStep,
  RosterValidationResult,
} from '../../models';

@Component({
  selector: 'app-process-designer-chat',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatChipsModule,
    MatTooltipModule,
    MatMenuModule,
    MatDialogModule,
    FlowStepEditorComponent,
  ],
  templateUrl: './process-designer-chat.component.html',
  styleUrl: './process-designer-chat.component.scss',
})
export class ProcessDesignerChatComponent implements OnInit, OnChanges, AfterViewChecked {
  @Input() team!: AgenticTeam;

  /**
   * Emitted every time the roster validation is (re)loaded — including `null` on
   * a load failure. Lets an embedding stage (Agent Studio Stage 3) track "is the
   * roster fully staffed" without re-fetching it independently.
   */
  @Output() readonly rosterChanged = new EventEmitter<RosterValidationResult | null>();

  @ViewChild('messagesContainer') messagesContainer!: ElementRef<HTMLDivElement>;
  @ViewChild('flowchartContainer') flowchartContainer!: ElementRef<HTMLDivElement>;

  private readonly api = inject(AgenticTeamApiService);
  private readonly fb = inject(FormBuilder);
  private readonly sanitizer = inject(DomSanitizer);
  private readonly dialog = inject(MatDialog);

  messages = signal<AgenticConversationMessage[]>([]);
  currentProcess = signal<ProcessDefinition | null>(null);
  suggestedQuestions = signal<string[]>([]);
  loading = signal(false);
  saving = signal(false);
  error = signal<string | null>(null);
  flowchartSvg = signal<SafeHtml | null>(null);

  // Interactive diagram state
  selectedStepId = signal<string | null>(null);
  selectedStep = signal<ProcessStep | null>(null);
  editingProcessMeta = signal(false);
  processNameEdit = signal('');
  processDescEdit = signal('');

  rosterAgents = signal<AgenticTeamAgent[]>([]);
  rosterValidation = signal<RosterValidationResult | null>(null);
  rosterLoading = signal(false);
  rosterActionError = signal<string | null>(null);
  expandedAgent = signal<string | null>(null);
  /** Monotonic stamp for `refreshRoster`; guards against out-of-order refresh results. */
  private rosterRefreshSeq = 0;

  private conversationId: string | null = null;
  /** Monotonic stamp for `startConversation`; guards against out-of-order createConversation results. */
  private conversationSeq = 0;
  private _stepCounter = 0;

  form = this.fb.nonNullable.group({
    message: ['', [Validators.required, Validators.minLength(1)]],
  });

  ngOnInit(): void {
    this.startConversation();
  }

  ngOnChanges(changes: SimpleChanges): void {
    const teamChange = changes['team'];
    // Restart the conversation only when the team *identity* changes, not on
    // every new object reference. An embedding stage (Agent Studio Stage 3)
    // re-fetches the team after roster edits and hands us a freshly-parsed
    // object with the same team_id; restarting on reference-equality alone would
    // reset the chat — and, because that restart re-emits `rosterChanged`, drive
    // the parent into an unbounded getTeam→re-render→restart loop.
    if (
      teamChange &&
      !teamChange.firstChange &&
      teamChange.previousValue?.team_id !== teamChange.currentValue?.team_id
    ) {
      this.startConversation();
    }
  }

  ngAfterViewChecked(): void {
    this.scrollToBottom();
    this.attachFlowchartClickHandlers();
  }

  private scrollToBottom(): void {
    if (this.messagesContainer?.nativeElement) {
      const el = this.messagesContainer.nativeElement;
      el.scrollTop = el.scrollHeight;
    }
  }

  private attachFlowchartClickHandlers(): void {
    if (!this.flowchartContainer?.nativeElement) return;
    const nodes = this.flowchartContainer.nativeElement.querySelectorAll('[data-step-id]');
    nodes.forEach((node: Element) => {
      if ((node as HTMLElement).dataset['bound']) return;
      (node as HTMLElement).dataset['bound'] = '1';
      node.addEventListener('click', (e) => {
        e.stopPropagation();
        const stepId = (node as HTMLElement).dataset['stepId'];
        if (stepId) this.onStepClick(stepId);
      });
    });
  }

  private startConversation(): void {
    // Sequence token: startConversation can be invoked again (e.g. rapid team
    // switching) before a prior createConversation call resolves. Stamp each
    // call and drop any callback whose stamp is no longer the latest, so a
    // slow older response can't complete last and overwrite the active
    // conversationId/messages/process state with stale data.
    const seq = ++this.conversationSeq;
    this.error.set(null);
    this.conversationId = null;
    this.messages.set([]);
    this.currentProcess.set(null);
    this.suggestedQuestions.set([]);
    this.selectedStepId.set(null);
    this.selectedStep.set(null);

    this.api.createConversation(this.team.team_id).subscribe({
      next: (res) => {
        if (seq !== this.conversationSeq) return; // superseded by a newer call
        this.applyState(res);
      },
      error: (err) => {
        if (seq !== this.conversationSeq) return;
        this.error.set(err?.error?.detail ?? 'Failed to start conversation');
      },
    });
  }

  private applyState(res: {
    conversation_id: string;
    messages: AgenticConversationMessage[];
    current_process: ProcessDefinition | null;
    suggested_questions: string[];
  }): void {
    this.conversationId = res.conversation_id;
    this.messages.set(res.messages);
    this.currentProcess.set(res.current_process);
    this.suggestedQuestions.set(res.suggested_questions);
    this.buildFlowchart(res.current_process);
    this.refreshRoster();
    // Refresh selected step if editor is open; clear if process is gone
    if (this.selectedStepId()) {
      if (res.current_process) {
        const step = res.current_process.steps.find((s) => s.step_id === this.selectedStepId());
        this.selectedStep.set(step ?? null);
        if (!step) {
          this.selectedStepId.set(null);
        }
      } else {
        this.selectedStepId.set(null);
        this.selectedStep.set(null);
      }
    }
  }

  refreshRoster(): void {
    // Sequence token: a roster mutation (add/delete/edit) can trigger a new
    // refresh while an older one is still in flight. Stamp each refresh and drop
    // any callback whose stamp is no longer the latest, so a slow older
    // validateRoster can't complete last and emit a stale is_fully_staffed —
    // which the embedding stage (Agent Studio Stage 3) would use to (wrongly)
    // enable "Test this team →" for a roster that has since changed.
    const seq = ++this.rosterRefreshSeq;
    this.rosterLoading.set(true);
    this.rosterActionError.set(null);
    this.api.listTeamAgents(this.team.team_id).subscribe({
      next: (agents) => {
        if (seq !== this.rosterRefreshSeq) return; // superseded by a newer refresh
        this.rosterAgents.set(agents);
        // Keep the loading indicator up until validation also resolves — the
        // roster isn't "fully loaded" until its staffing gaps are known.
        this.api.validateRoster(this.team.team_id).subscribe({
          next: (result) => {
            if (seq !== this.rosterRefreshSeq) return;
            this.rosterValidation.set(result);
            this.rosterLoading.set(false);
            this.rosterChanged.emit(result);
          },
          error: (err) => {
            if (seq !== this.rosterRefreshSeq) return;
            this.rosterValidation.set(null);
            this.rosterLoading.set(false);
            this.rosterChanged.emit(null);
            // Surface the failure too: clearing the gate silently disables the
            // embedding stage's "Test this team →" with no explanation otherwise.
            this.rosterActionError.set(err?.error?.detail ?? 'Failed to validate the roster');
          },
        });
      },
      error: (err) => {
        if (seq !== this.rosterRefreshSeq) return;
        // Surface the failure instead of silently leaving a stale roster: the
        // user needs to know their view may be out of date. Also clear the
        // validation and emit null so an embedding stage (Agent Studio Stage 3)
        // drops its "fully staffed" gate — otherwise a failed refresh after a
        // previously-staffed load would keep "Test this team →" enabled on stale
        // staffing (rosterFullyStaffed is only updated from rosterChanged).
        this.rosterLoading.set(false);
        this.rosterValidation.set(null);
        this.rosterChanged.emit(null);
        this.rosterActionError.set(err?.error?.detail ?? 'Failed to load roster');
      },
    });
  }

  toggleAgentExpand(agentName: string): void {
    this.expandedAgent.update((current) => (current === agentName ? null : agentName));
  }

  gapCountForAgent(agentName: string): number {
    const v = this.rosterValidation();
    if (!v) return 0;
    return v.gaps.filter((g) => g.agent_name === agentName).length;
  }

  // ---------------------------------------------------------------------------
  // Roster mutation: add from registry / suggest via chat / delete
  // (spec §3, Stage 3 "Roster panel")
  // ---------------------------------------------------------------------------

  /** Open the "search registry agents" dialog and add the chosen manifest. */
  openAddFromRegistry(): void {
    this.rosterActionError.set(null);
    const data: AddAgentFromRegistryDialogData = {
      existingManifestIds: this.rosterAgents()
        .filter((a) => a.source === 'registry')
        .map((a) => a.manifest_id),
    };
    const ref = this.dialog.open<
      AddAgentFromRegistryDialogComponent,
      AddAgentFromRegistryDialogData,
      AddAgentFromRegistryDialogResult
    >(AddAgentFromRegistryDialogComponent, { data, width: '480px' });
    ref.afterClosed().subscribe((manifestId) => this.onAddFromRegistryDialogClosed(manifestId));
  }

  /** Public for unit tests; invoked by `openAddFromRegistry` after the dialog closes. */
  onAddFromRegistryDialogClosed(manifestId: AddAgentFromRegistryDialogResult | undefined): void {
    if (!manifestId) return;
    this.api.addAgentFromRegistry(this.team.team_id, manifestId).subscribe({
      next: () => this.refreshRoster(),
      error: (err) => {
        this.rosterActionError.set(err?.error?.detail ?? 'Failed to add agent from registry');
      },
    });
  }

  /** "Suggest via chat": seed the chat input with a prompt asking for a new agent. */
  suggestAgentViaChat(): void {
    this.form.patchValue({ message: 'Suggest an additional agent for this team.' });
  }

  deleteAgent(agent: AgenticTeamAgent, event: Event): void {
    event.stopPropagation();
    // Use the shared Material confirm dialog (danger variant) rather than the
    // native window.confirm, so a destructive roster removal matches the rest of
    // the app's theming and Cancel-focused destructive-prompt convention.
    const ref = this.dialog.open<
      ConfirmDialogComponent,
      ConfirmDialogData,
      boolean
    >(ConfirmDialogComponent, {
      data: {
        title: 'Remove agent',
        message: `Remove "${agent.agent_name}" from the roster?`,
        confirmLabel: 'Remove',
        cancelLabel: 'Cancel',
        variant: 'danger',
      },
      width: '420px',
    });
    ref.afterClosed().subscribe((confirmed) => this.onDeleteAgentConfirmed(agent, confirmed));
  }

  /** Public for unit tests; invoked by `deleteAgent` after the confirm dialog closes. */
  onDeleteAgentConfirmed(agent: AgenticTeamAgent, confirmed: boolean | undefined): void {
    if (!confirmed) return;
    this.rosterActionError.set(null);
    this.api.removeTeamAgent(this.team.team_id, agent.agent_name).subscribe({
      next: () => this.refreshRoster(),
      error: (err) => {
        this.rosterActionError.set(err?.error?.detail ?? 'Failed to remove agent');
      },
    });
  }

  onSubmit(): void {
    if (this.form.invalid || this.loading()) return;
    const message = this.form.getRawValue().message.trim();
    if (!message) return;
    this.sendMessage(message);
  }

  onSuggestedQuestion(q: string): void {
    this.sendMessage(q);
  }

  private sendMessage(message: string): void {
    if (!this.conversationId) return;

    this.form.reset({ message: '' });
    this.messages.update((msgs) => [
      ...msgs,
      { role: 'user' as const, content: message, timestamp: new Date().toISOString() },
    ]);
    this.loading.set(true);
    this.error.set(null);

    this.api.sendMessage(this.conversationId, message).subscribe({
      next: (res) => {
        this.applyState(res);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(err?.error?.detail ?? 'Failed to send message');
        this.loading.set(false);
      },
    });
  }

  newConversation(): void {
    this.startConversation();
  }

  formatTime(timestamp: string): string {
    if (!timestamp) return '';
    try {
      return new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch {
      return '';
    }
  }

  // ---------------------------------------------------------------------------
  // Interactive diagram: process CRUD
  // ---------------------------------------------------------------------------

  createNewProcess(): void {
    this.saving.set(true);
    this.api.createProcess(this.team.team_id).subscribe({
      next: (process) => {
        this.currentProcess.set(process);
        this.buildFlowchart(process);
        this.saving.set(false);
        // Link the new process to the active conversation so chat stays in sync
        if (this.conversationId) {
          this.api.setConversationProcess(this.conversationId, process.process_id).subscribe({
            error: (err) => this.error.set(err?.error?.detail ?? 'Failed to link process to conversation'),
          });
        }
      },
      error: (err) => {
        this.error.set(err?.error?.detail ?? 'Failed to create process');
        this.saving.set(false);
      },
    });
  }

  addStep(stepType: 'action' | 'decision' = 'action'): void {
    const process = this.currentProcess();
    if (!process) return;

    this._stepCounter++;
    const newStep: ProcessStep = {
      step_id: `step_${Date.now()}_${this._stepCounter}`,
      name: stepType === 'decision' ? 'New Decision' : 'New Step',
      description: '',
      step_type: stepType,
      agents: [],
      next_steps: [],
      condition: null,
    };

    // Wire up: last step → new step
    const updatedSteps = [...process.steps];
    if (updatedSteps.length > 0) {
      const lastStep = { ...updatedSteps[updatedSteps.length - 1] };
      if (lastStep.next_steps.length === 0) {
        lastStep.next_steps = [newStep.step_id];
        updatedSteps[updatedSteps.length - 1] = lastStep;
      }
    }
    updatedSteps.push(newStep);

    const updated = { ...process, steps: updatedSteps };
    this.currentProcess.set(updated);
    this.buildFlowchart(updated);
    this.saveProcess(updated, process);
    this.onStepClick(newStep.step_id);
  }

  onStepClick(stepId: string): void {
    const process = this.currentProcess();
    if (!process) return;
    const step = process.steps.find((s) => s.step_id === stepId);
    if (!step) return;
    this.selectedStepId.set(stepId);
    this.selectedStep.set({ ...step });
    this.buildFlowchart(process); // re-render to highlight selected
  }

  onStepUpdated(updatedStep: ProcessStep): void {
    const process = this.currentProcess();
    if (!process) return;

    const updatedSteps = process.steps.map((s) =>
      s.step_id === updatedStep.step_id ? updatedStep : s,
    );
    const updated = { ...process, steps: updatedSteps };
    this.currentProcess.set(updated);
    this.selectedStep.set({ ...updatedStep });
    this.buildFlowchart(updated);
    this.saveProcess(updated, process);
  }

  onStepDeleted(stepId: string): void {
    const process = this.currentProcess();
    if (!process) return;

    // Remove step and clean up references
    const updatedSteps = process.steps
      .filter((s) => s.step_id !== stepId)
      .map((s) => ({
        ...s,
        next_steps: s.next_steps.filter((ns) => ns !== stepId),
      }));
    const updated = { ...process, steps: updatedSteps };
    this.currentProcess.set(updated);
    this.selectedStepId.set(null);
    this.selectedStep.set(null);
    this.buildFlowchart(updated);
    this.saveProcess(updated, process);
  }

  onStepEditorClosed(): void {
    this.selectedStepId.set(null);
    this.selectedStep.set(null);
    this.buildFlowchart(this.currentProcess());
  }

  startEditProcessMeta(): void {
    const process = this.currentProcess();
    if (!process) return;
    this.processNameEdit.set(process.name);
    this.processDescEdit.set(process.description);
    this.editingProcessMeta.set(true);
  }

  saveProcessMeta(): void {
    const process = this.currentProcess();
    if (!process) return;
    const updated = {
      ...process,
      name: this.processNameEdit(),
      description: this.processDescEdit(),
    };
    this.currentProcess.set(updated);
    this.editingProcessMeta.set(false);
    this.saveProcess(updated, process);
  }

  cancelEditProcessMeta(): void {
    this.editingProcessMeta.set(false);
  }

  private saveProcess(process: ProcessDefinition, previous: ProcessDefinition | null): void {
    this.saving.set(true);
    this.api.updateProcess(process.process_id, process).subscribe({
      next: () => {
        this.saving.set(false);
        this.refreshRoster();
      },
      error: (err) => {
        this.currentProcess.set(previous);
        this.buildFlowchart(previous);
        this.error.set(err?.error?.detail ?? 'Failed to save process');
        this.saving.set(false);
      },
    });
  }

  // ---------------------------------------------------------------------------
  // Flowchart SVG builder
  // ---------------------------------------------------------------------------

  /**
   * Build a custom interactive SVG flowchart from the process definition.
   * Nodes are interactive — clicking them opens the step editor.
   */
  private buildFlowchart(process: ProcessDefinition | null): void {
    if (!process || process.steps.length === 0) {
      this.flowchartSvg.set(null);
      return;
    }

    const steps = process.steps;
    const nodeSpacingY = 100;
    const nodeWidth = 200;
    const nodeHeight = 50;
    const padding = 40;
    const svgWidth = nodeWidth + padding * 2;
    const selectedId = this.selectedStepId();

    // Build a map from step_id to index for layout
    const idxMap = new Map<string, number>();
    steps.forEach((s, i) => idxMap.set(s.step_id, i));

    // Layout: one trigger node at top, then steps vertically, then output at bottom
    const totalNodes = steps.length + 2; // trigger + steps + output
    const svgHeight = totalNodes * nodeSpacingY + padding * 2;

    const cx = svgWidth / 2;
    let svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${svgWidth} ${svgHeight}" width="100%" height="100%">`;
    svg += `<defs>
      <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
        <polygon points="0 0, 10 3.5, 0 7" fill="#58a6ff"/>
      </marker>
      <filter id="glow">
        <feGaussianBlur stdDeviation="3" result="blur"/>
        <feMerge>
          <feMergeNode in="blur"/>
          <feMergeNode in="SourceGraphic"/>
        </feMerge>
      </filter>
    </defs>`;

    // Helper: node Y position
    const nodeY = (idx: number) => padding + idx * nodeSpacingY;

    // Draw trigger node (rounded rect, green tint)
    const trigY = nodeY(0);
    svg += `<rect x="${cx - nodeWidth / 2}" y="${trigY}" width="${nodeWidth}" height="${nodeHeight}" rx="25" ry="25" fill="#1a3a2a" stroke="#3fb950" stroke-width="1.5"/>`;
    svg += `<text x="${cx}" y="${trigY + nodeHeight / 2 + 5}" text-anchor="middle" fill="#3fb950" font-size="12" font-family="sans-serif">${this.escSvg(process.trigger.trigger_type.toUpperCase())}: ${this.truncate(process.trigger.description || 'Trigger', 20)}</text>`;

    // Arrow from trigger to first step
    if (steps.length > 0) {
      svg += this.arrow(cx, trigY + nodeHeight, cx, nodeY(1));
    }

    // Draw step nodes
    steps.forEach((step, i) => {
      const y = nodeY(i + 1);
      const isDecision = step.step_type === 'decision';
      const isSelected = step.step_id === selectedId;
      const hasNoAgents = step.agents.length === 0;

      // Clickable group
      svg += `<g data-step-id="${this.escSvg(step.step_id)}" class="flowchart-node" style="cursor:pointer">`;

      if (isDecision) {
        // Diamond shape
        const hw = nodeWidth / 2;
        const hh = nodeHeight / 2;
        const dmx = cx;
        const dmy = y + hh;
        const strokeColor = isSelected ? '#f0f6fc' : '#bc8cff';
        const strokeWidth = isSelected ? '2.5' : '1.5';
        const filter = isSelected ? ' filter="url(#glow)"' : '';
        svg += `<polygon points="${dmx},${y} ${dmx + hw},${dmy} ${dmx},${y + nodeHeight} ${dmx - hw},${dmy}" fill="#2d1b3d" stroke="${strokeColor}" stroke-width="${strokeWidth}"${filter}/>`;
        svg += `<text x="${cx}" y="${dmy + 4}" text-anchor="middle" fill="#bc8cff" font-size="11" font-family="sans-serif">${this.escSvg(this.truncate(step.name, 22))}</text>`;
      } else {
        const strokeColor = isSelected ? '#f0f6fc' : '#58a6ff';
        const strokeWidth = isSelected ? '2.5' : '1.5';
        const filter = isSelected ? ' filter="url(#glow)"' : '';
        svg += `<rect x="${cx - nodeWidth / 2}" y="${y}" width="${nodeWidth}" height="${nodeHeight}" rx="8" ry="8" fill="#161b22" stroke="${strokeColor}" stroke-width="${strokeWidth}"${filter}/>`;
        svg += `<text x="${cx}" y="${y + 20}" text-anchor="middle" fill="#f0f6fc" font-size="12" font-family="sans-serif">${this.escSvg(this.truncate(step.name, 22))}</text>`;

        // Show agent names below step name
        if (step.agents.length > 0) {
          const agentLabel = step.agents.map((a) => a.agent_name).join(', ');
          svg += `<text x="${cx}" y="${y + 36}" text-anchor="middle" fill="#8b949e" font-size="10" font-family="sans-serif">${this.escSvg(this.truncate(agentLabel, 28))}</text>`;
        }
      }

      // Warning indicator for steps with no agents
      if (hasNoAgents) {
        svg += `<circle cx="${cx + nodeWidth / 2 - 8}" cy="${y + 8}" r="6" fill="#d29922"/>`;
        svg += `<text x="${cx + nodeWidth / 2 - 8}" y="${y + 12}" text-anchor="middle" fill="#0d1117" font-size="9" font-weight="bold" font-family="sans-serif">!</text>`;
      }

      svg += `</g>`;

      // Arrows to next steps
      for (const nextId of step.next_steps) {
        const nextIdx = idxMap.get(nextId);
        if (nextIdx !== undefined) {
          svg += this.arrow(cx, y + nodeHeight, cx, nodeY(nextIdx + 1));
        }
      }

      // If no explicit next and it's the last step, arrow to output
      if (step.next_steps.length === 0 && i === steps.length - 1) {
        svg += this.arrow(cx, y + nodeHeight, cx, nodeY(steps.length + 1));
      }
    });

    // Draw output node (rounded rect, orange tint)
    const outY = nodeY(steps.length + 1);
    svg += `<rect x="${cx - nodeWidth / 2}" y="${outY}" width="${nodeWidth}" height="${nodeHeight}" rx="25" ry="25" fill="#3d2b1a" stroke="#d29922" stroke-width="1.5"/>`;
    svg += `<text x="${cx}" y="${outY + nodeHeight / 2 + 5}" text-anchor="middle" fill="#d29922" font-size="12" font-family="sans-serif">${this.escSvg(this.truncate(process.output.description || 'Output', 22))}</text>`;

    svg += '</svg>';
    this.flowchartSvg.set(this.sanitizer.bypassSecurityTrustHtml(svg));
  }

  private arrow(x1: number, y1: number, x2: number, y2: number): string {
    return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2 - 4}" stroke="#58a6ff" stroke-width="1.5" marker-end="url(#arrowhead)"/>`;
  }

  private truncate(text: string, max: number): string {
    return text.length > max ? text.substring(0, max - 1) + '\u2026' : text;
  }

  private escSvg(text: string): string {
    return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
}
