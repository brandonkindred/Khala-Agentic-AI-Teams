import {
  Component,
  EventEmitter,
  Input,
  OnInit,
  OnChanges,
  OnDestroy,
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
import { Subject, takeUntil } from 'rxjs';
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
import { LatestOnly } from '../../shared/latest-only';
import { extractErrorDetail } from '../../shared/extract-error-detail';
import type {
  AgenticTeam,
  AgenticTeamAgent,
  AgenticConversationMessage,
  ProcessDefinition,
  ProcessStep,
  RosterValidationResult,
  UpdateAgentRequest,
} from '../../models';

/** Chat prompt seeded by the roster panel's "Suggest via chat" action. */
const SUGGEST_AGENT_PROMPT = 'Suggest an additional agent for this team.';

/** A roster agent's fields, editable via the inline "Edit" affordance. */
interface AgentEditDraft {
  role: string;
  skills: string;
  capabilities: string;
  tools: string;
  expertise: string;
}

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
export class ProcessDesignerChatComponent implements OnInit, OnChanges, AfterViewChecked, OnDestroy {
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
  // Completes on destroy; every subscription in this component is gated on it
  // so a late response can't mutate a torn-down component.
  private readonly destroy$ = new Subject<void>();

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
  /** Name of the roster agent currently in inline-edit mode (`null` if none). */
  editingAgent = signal<string | null>(null);
  editDraft = signal<AgentEditDraft>({ role: '', skills: '', capabilities: '', tools: '', expertise: '' });
  /**
   * Snapshot of the row's fields as the edit form opened, used to send only the
   * fields the user actually changed on save (see `saveAgentEdits`). `null` when
   * no edit is in progress.
   */
  private readonly editOriginal = signal<AgentEditDraft | null>(null);
  /** Guards `refreshRoster` against out-of-order refresh results. */
  private readonly rosterRefreshGuard = new LatestOnly();

  /**
   * Click listeners bound by `attachFlowchartClickHandlers`, tracked so they
   * can be explicitly removed (see `detachFlowchartClickHandlers`) instead of
   * being silently discarded whenever the flowchart SVG is replaced.
   */
  private readonly flowchartClickListeners: { node: HTMLElement; listener: (e: Event) => void }[] = [];

  private conversationId: string | null = null;
  /** Monotonic stamp for `startConversation`; guards against out-of-order createConversation results. */
  private conversationSeq = 0;
  private _stepCounter = 0;

  form = this.fb.nonNullable.group({
    message: ['', [Validators.required]],
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

  /** Distance (px) from the bottom within which the user is considered "at the bottom" for auto-scroll purposes. */
  private static readonly SCROLL_BOTTOM_THRESHOLD_PX = 48;

  /**
   * Set by a message-add site (`sendMessage`, `applyState`) that determined the
   * user was near the bottom before the mutation; consumed by the next
   * `ngAfterViewChecked` — the point at which the newly-added message's DOM has
   * actually been laid out — and cleared so later view-checked cycles with no
   * new message don't force a scroll.
   */
  private pendingScrollToBottom = false;

  ngAfterViewChecked(): void {
    if (this.pendingScrollToBottom) {
      this.pendingScrollToBottom = false;
      this.scrollToBottom();
    }
    this.attachFlowchartClickHandlers();
  }

  /**
   * Postconditions: completes `destroy$` (unsubscribing every
   * `takeUntil(this.destroy$)` stream) and detaches every flowchart click
   * listener bound by `attachFlowchartClickHandlers`, so nothing keeps a
   * closure over a destroyed component.
   */
  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
    this.detachFlowchartClickHandlers();
  }

  /**
   * Whether the messages container is scrolled at (or within threshold of) its
   * bottom. Used to decide whether a newly-added message should auto-scroll the
   * view, so a user who has scrolled up to read history isn't yanked back down.
   */
  private isNearBottom(): boolean {
    const el = this.messagesContainer?.nativeElement;
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight < ProcessDesignerChatComponent.SCROLL_BOTTOM_THRESHOLD_PX;
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
      const listener = (e: Event) => {
        e.stopPropagation();
        const stepId = (node as HTMLElement).dataset['stepId'];
        if (stepId) this.onStepClick(stepId);
      };
      node.addEventListener('click', listener);
      this.flowchartClickListeners.push({ node: node as HTMLElement, listener });
    });
  }

  /**
   * Remove every click listener bound by `attachFlowchartClickHandlers` and
   * clear their `data-bound` markers. Called before each `buildFlowchart`
   * replaces the flowchart's DOM (so the outgoing nodes' listeners don't
   * linger) and from `ngOnDestroy` (so the component doesn't leave listeners
   * closing over `this` attached to nodes still in the document).
   */
  private detachFlowchartClickHandlers(): void {
    for (const { node, listener } of this.flowchartClickListeners) {
      node.removeEventListener('click', listener);
      delete node.dataset['bound'];
    }
    this.flowchartClickListeners.length = 0;
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

    this.api.createConversation(this.team.team_id).pipe(takeUntil(this.destroy$)).subscribe({
      next: (res) => {
        if (seq !== this.conversationSeq) return; // superseded by a newer call
        this.applyState(res);
      },
      error: (err) => {
        if (seq !== this.conversationSeq) return;
        this.error.set(extractErrorDetail(err, 'Failed to start conversation'));
      },
    });
  }

  private applyState(res: {
    conversation_id: string;
    messages: AgenticConversationMessage[];
    current_process: ProcessDefinition | null;
    suggested_questions: string[];
  }): void {
    // Capture before mutating: a new message should only auto-scroll the view
    // when the user hadn't already scrolled away from the bottom to read history.
    if (res.messages.length > this.messages().length && this.isNearBottom()) {
      this.pendingScrollToBottom = true;
    }
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

  /**
   * Reload the roster and its staffing validation from the backend.
   *
   * Preconditions: `this.team.team_id` is set.
   * Postconditions: on success, `rosterAgents` and `rosterValidation` reflect
   * the latest backend state and `rosterChanged` has been emitted with the
   * new validation result. On failure, `rosterActionError` carries a message
   * and `rosterChanged` is emitted with `null` so an embedding stage's
   * "fully staffed" gate can't act on stale staffing.
   * Invariant: only the most recently issued refresh (via `rosterRefreshGuard`)
   * is allowed to apply its result, so an in-flight refresh superseded by a
   * newer one is dropped rather than overwriting fresher state.
   */
  refreshRoster(): void {
    // Sequence token: a roster mutation (add/delete/edit) can trigger a new
    // refresh while an older one is still in flight. Stamp each refresh and drop
    // any callback whose stamp is no longer the latest, so a slow older
    // validateRoster can't complete last and emit a stale is_fully_staffed —
    // which the embedding stage (Agent Studio Stage 3) would use to (wrongly)
    // enable "Test this team →" for a roster that has since changed.
    const token = this.rosterRefreshGuard.next();
    this.rosterLoading.set(true);
    this.rosterActionError.set(null);
    this.api.listTeamAgents(this.team.team_id).pipe(takeUntil(this.destroy$)).subscribe({
      next: (agents) => {
        if (!this.rosterRefreshGuard.isCurrent(token)) return; // superseded by a newer refresh
        this.rosterAgents.set(agents);
        // Keep the loading indicator up until validation also resolves — the
        // roster isn't "fully loaded" until its staffing gaps are known.
        this.api.validateRoster(this.team.team_id).pipe(takeUntil(this.destroy$)).subscribe({
          next: (result) => {
            if (!this.rosterRefreshGuard.isCurrent(token)) return;
            this.rosterValidation.set(result);
            this.rosterLoading.set(false);
            this.rosterChanged.emit(result);
          },
          error: (err) => {
            if (!this.rosterRefreshGuard.isCurrent(token)) return;
            this.rosterValidation.set(null);
            this.rosterLoading.set(false);
            this.rosterChanged.emit(null);
            // Surface the failure too: clearing the gate silently disables the
            // embedding stage's "Test this team →" with no explanation otherwise.
            this.rosterActionError.set(extractErrorDetail(err, 'Failed to validate the roster'));
          },
        });
      },
      error: (err) => {
        if (!this.rosterRefreshGuard.isCurrent(token)) return;
        // Surface the failure instead of silently leaving a stale roster: the
        // user needs to know their view may be out of date. Also clear the
        // validation and emit null so an embedding stage (Agent Studio Stage 3)
        // drops its "fully staffed" gate — otherwise a failed refresh after a
        // previously-staffed load would keep "Test this team →" enabled on stale
        // staffing (rosterFullyStaffed is only updated from rosterChanged).
        this.rosterLoading.set(false);
        this.rosterValidation.set(null);
        this.rosterChanged.emit(null);
        this.rosterActionError.set(extractErrorDetail(err, 'Failed to load roster'));
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
  // Roster mutation: add from registry / suggest via chat / delete / inline edit
  // (spec §3, Stage 3 "Roster panel")
  // ---------------------------------------------------------------------------

  /** Open the "search registry agents" dialog and add the chosen manifest. */
  openAddFromRegistry(): void {
    this.rosterActionError.set(null);
    const data: AddAgentFromRegistryDialogData = {
      existingManifestIds: this.rosterAgents()
        .map((a) => a.manifest_id)
        .filter((id): id is string => !!id),
    };
    const ref = this.dialog.open<
      AddAgentFromRegistryDialogComponent,
      AddAgentFromRegistryDialogData,
      AddAgentFromRegistryDialogResult
    >(AddAgentFromRegistryDialogComponent, { data, width: '480px' });
    ref
      .afterClosed()
      .pipe(takeUntil(this.destroy$))
      .subscribe((manifestId) => this.onAddFromRegistryDialogClosed(manifestId));
  }

  /** Public for unit tests; invoked by `openAddFromRegistry` after the dialog closes. */
  onAddFromRegistryDialogClosed(manifestId: AddAgentFromRegistryDialogResult | undefined): void {
    if (!manifestId) return;
    this.api.addAgentFromRegistry(this.team.team_id, manifestId).pipe(takeUntil(this.destroy$)).subscribe({
      next: () => this.refreshRoster(),
      error: (err) => {
        this.rosterActionError.set(extractErrorDetail(err, 'Failed to add agent from registry'));
      },
    });
  }

  /** "Suggest via chat": seed the chat input with a prompt asking for a new agent. */
  suggestAgentViaChat(): void {
    this.form.patchValue({ message: SUGGEST_AGENT_PROMPT });
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
    ref
      .afterClosed()
      .pipe(takeUntil(this.destroy$))
      .subscribe((confirmed) => this.onDeleteAgentConfirmed(agent, confirmed));
  }

  /** Public for unit tests; invoked by `deleteAgent` after the confirm dialog closes. */
  onDeleteAgentConfirmed(agent: AgenticTeamAgent, confirmed: boolean | undefined): void {
    if (!confirmed) return;
    this.rosterActionError.set(null);
    this.api.removeTeamAgent(this.team.team_id, agent.agent_name).pipe(takeUntil(this.destroy$)).subscribe({
      next: () => {
        if (this.editingAgent() === agent.agent_name) this.editingAgent.set(null);
        this.refreshRoster();
      },
      error: (err) => {
        this.rosterActionError.set(extractErrorDetail(err, 'Failed to remove agent'));
      },
    });
  }

  startEditAgent(agent: AgenticTeamAgent, event: Event): void {
    event.stopPropagation();
    this.rosterActionError.set(null);
    const snapshot: AgentEditDraft = {
      role: agent.role,
      skills: agent.skills.join(', '),
      capabilities: agent.capabilities.join(', '),
      tools: agent.tools.join(', '),
      expertise: agent.expertise.join(', '),
    };
    this.editDraft.set({ ...snapshot });
    this.editOriginal.set(snapshot);
    this.editingAgent.set(agent.agent_name);
  }

  cancelEditAgent(event: Event): void {
    event.stopPropagation();
    this.editingAgent.set(null);
    this.editOriginal.set(null);
  }

  /** Update a single field of the in-progress edit draft (template can't spread). */
  updateEditDraftField(field: keyof AgentEditDraft, value: string): void {
    this.editDraft.update((draft) => ({ ...draft, [field]: value }));
  }

  saveAgentEdits(agent: AgenticTeamAgent, event: Event): void {
    event.stopPropagation();
    const draft = this.editDraft();
    const original = this.editOriginal();
    const toList = (s: string) =>
      s.split(',').map((v) => v.trim()).filter((v) => v.length > 0);

    // Send ONLY the fields the user actually changed vs. what the form opened
    // with. The backend PUT is a partial update (exclude_unset), so omitting an
    // untouched field preserves whatever newer value the chat or another roster
    // mutation wrote for it while this form was open — a full-object save would
    // clobber those with the stale draft. Raw-string compare suffices: an
    // untouched field is byte-identical to its `startEditAgent` snapshot. With no
    // baseline (a save racing the form close) fall back to sending the field so a
    // real edit isn't silently dropped.
    const changed = (field: keyof AgentEditDraft) => !original || draft[field] !== original[field];
    const updates: UpdateAgentRequest = {};
    if (changed('role')) updates.role = draft.role.trim();
    if (changed('skills')) updates.skills = toList(draft.skills);
    if (changed('capabilities')) updates.capabilities = toList(draft.capabilities);
    if (changed('tools')) updates.tools = toList(draft.tools);
    if (changed('expertise')) updates.expertise = toList(draft.expertise);

    // Nothing changed → close the form without a redundant write.
    if (Object.keys(updates).length === 0) {
      this.editingAgent.set(null);
      this.editOriginal.set(null);
      return;
    }
    this.rosterActionError.set(null);
    this.api
      .updateTeamAgent(this.team.team_id, agent.agent_name, updates)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: () => {
          this.editingAgent.set(null);
          this.editOriginal.set(null);
          this.refreshRoster();
        },
        error: (err) => {
          this.rosterActionError.set(extractErrorDetail(err, 'Failed to update agent'));
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
    // Guard here (not just in onSubmit) so onSuggestedQuestion — which bypasses
    // the form — can't fire a second concurrent send while one is in flight.
    if (!this.conversationId || this.loading()) return;

    this.form.reset({ message: '' });
    const optimisticMessage: AgenticConversationMessage = {
      role: 'user',
      content: message,
      timestamp: new Date().toISOString(),
    };
    if (this.isNearBottom()) this.pendingScrollToBottom = true;
    this.messages.update((msgs) => [...msgs, optimisticMessage]);
    this.loading.set(true);
    this.error.set(null);

    this.api.sendMessage(this.conversationId, message).pipe(takeUntil(this.destroy$)).subscribe({
      next: (res) => {
        this.applyState(res);
        this.loading.set(false);
      },
      error: (err) => {
        // The backend persists the turn only once the LLM call and roster/process
        // save succeed (see agentic_team_provisioning's send_message); on failure
        // nothing was saved, so roll back the optimistic append rather than leave
        // a message visible that a refresh would show was never sent.
        this.messages.update((msgs) => msgs.filter((m) => m !== optimisticMessage));
        this.error.set(extractErrorDetail(err, 'Failed to send message'));
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
    this.api.createProcess(this.team.team_id).pipe(takeUntil(this.destroy$)).subscribe({
      next: (process) => {
        this.currentProcess.set(process);
        this.buildFlowchart(process);
        this.saving.set(false);
        // Link the new process to the active conversation so chat stays in sync
        if (this.conversationId) {
          this.api
            .setConversationProcess(this.conversationId, process.process_id)
            .pipe(takeUntil(this.destroy$))
            .subscribe({
              error: (err) => this.error.set(extractErrorDetail(err, 'Failed to link process to conversation')),
            });
        }
      },
      error: (err) => {
        this.error.set(extractErrorDetail(err, 'Failed to create process'));
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
    this.api.updateProcess(process.process_id, process).pipe(takeUntil(this.destroy$)).subscribe({
      next: () => {
        this.saving.set(false);
        this.refreshRoster();
      },
      error: (err) => {
        this.currentProcess.set(previous);
        this.buildFlowchart(previous);
        this.error.set(extractErrorDetail(err, 'Failed to save process'));
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
   *
   * Postconditions: `flowchartSvg` is `null` when `process` is null or has no
   * steps; otherwise it holds a `SafeHtml` built from a hand-rolled SVG
   * string via `sanitizer.bypassSecurityTrustHtml`, rendered unsanitized via
   * `[innerHTML]`. Every process-derived value interpolated into that string
   * (trigger type/description, step id/name, agent names, output
   * description) MUST be passed through `escSvg` first — these values can
   * originate from LLM-generated process definitions seeded by untrusted
   * chat input, so an unescaped field here is a live stored-XSS vector, not
   * just a display bug. Any new dynamic field added to this method must be
   * wrapped in `escSvg` and covered by the tests in
   * `describe('flowchart SVG escaping (XSS hardening)', ...)`.
   */
  private buildFlowchart(process: ProcessDefinition | null): void {
    // The outgoing SVG (if any) is about to be discarded via [innerHTML] —
    // detach its listeners now rather than relying on the DOM nodes becoming
    // unreferenced garbage.
    this.detachFlowchartClickHandlers();
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
    svg += `<text x="${cx}" y="${trigY + nodeHeight / 2 + 5}" text-anchor="middle" fill="#3fb950" font-size="12" font-family="sans-serif">${this.escSvg(process.trigger.trigger_type.toUpperCase())}: ${this.escSvg(this.truncate(process.trigger.description || 'Trigger', 20))}</text>`;

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

  /**
   * Escape a plain-text value for interpolation into the hand-rolled SVG
   * markup built by `buildFlowchart`.
   *
   * Precondition: `text` is plain text, not markup.
   * Postcondition: returns `text` with `& < > " '` replaced by HTML
   * entities. `buildFlowchart` trusts the assembled SVG string via
   * `bypassSecurityTrustHtml`, so every value that reaches the template
   * MUST be passed through this helper — a value that skips it is an XSS
   * vector.
   */
  private escSvg(text: string): string {
    return text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }
}
