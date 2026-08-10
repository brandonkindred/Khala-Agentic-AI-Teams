import { ChangeDetectionStrategy, Component, DestroyRef, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { A11yModule } from '@angular/cdk/a11y';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { AgentDefinition, BUILD_SUB_STAGES, SaveAgentRequest } from '../../../models/agent-studio.model';
import { AgentCatalogComponent } from '../agent-console/agent-catalog/agent-catalog.component';
import { AgentProvisioningPanelComponent } from '../agent-provisioning-panel/agent-provisioning-panel.component';
import { AgentStudioApiService } from '../../../services/agent-studio-api.service';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { AgentStudioStagePlaceholderComponent } from './agent-studio-stage-placeholder.component';

/**
 * Agent Studio — Stage 1 "Build Agent" (spec §3, Stage 1). The entry point of
 * the journey. Build is itself a forward-only three-step sub-stepper — 1.1
 * Start → 1.2 Define → 1.3 Configure — shown here beneath the main stepper;
 * this component owns the sub-stepper chrome and its navigation rules.
 *
 * **1.1 Start** reuses the Agent Console catalog as-is (`app-agent-catalog`)
 * for browse / filter / inspect-drawer. Selecting an agent there (the
 * drawer's run action) clones it into a Stage-1 draft via
 * `AgentStudioApiService.cloneFromRegistry` — the source registry agent is
 * never mutated; an explicit "Continue to Define →" then advances the
 * sub-stepper. **1.2 Define** and **1.3 Configure** are scaffolded with the
 * shared stage placeholder (the build assistant and the full anatomy review
 * are a later increment); each carries its own forward chrome, and Configure
 * additionally carries the sub-stepper's one backward affordance
 * (`◂ back to Define`) and the real **"Save agent"** action, which calls
 * `AgentStudioApiService.saveAgent` and writes the resulting id to
 * `registryAgentId` (spec §3, Stage 1) — this is what unlocks the
 * main-stepper's gated "Test this agent →" (Stage 1 → Stage 2).
 *
 * Provisioning is folded into this stage (spec §3, Stage 1): a "Provision an
 * agent" affordance opens a slide-out hosting `AgentProvisioningPanelComponent`
 * — the same route-agnostic provisioning chat + job-status panel that
 * `AgentProvisioningDashboardComponent` embeds in its own "Provision" tab
 * (spec §4.1's Stage 1 adaptation caveat: one shared panel, reused by both,
 * rather than a second copy of the chat config and polling behavior). This
 * slide-out mounts only the panel, without `AgentProvisioningDashboardComponent`'s
 * `DashboardShellComponent` page chrome or its `ActivatedRoute`-based
 * `?jobId=` deep link, neither of which apply on this route. The full
 * dashboard is untouched and still used as-is in Agent Console's own
 * "Provisioning & Environments" tab. The panel is self-contained, so it is
 * only mounted while the slide-out is open. The slide-out itself is a proper
 * modal: a CDK focus trap with auto-capture moves focus into the panel, keeps
 * Tab cycling inside it, and restores focus to the trigger on close; Escape
 * dismisses it.
 */
@Component({
  selector: 'app-agent-studio-build-agent',
  standalone: true,
  imports: [
    A11yModule,
    MatButtonModule,
    MatIconModule,
    AgentCatalogComponent,
    AgentProvisioningPanelComponent,
    AgentStudioStagePlaceholderComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './agent-studio-build-agent.component.html',
  styleUrl: './agent-studio-build-agent.component.scss',
})
export class AgentStudioBuildAgentComponent {
  readonly state = inject(AgentStudioStateService);
  private readonly api = inject(AgentStudioApiService);
  private readonly destroyRef = inject(DestroyRef);

  /** The forward-only 1.1/1.2/1.3 sub-stage list rendered by the sub-stepper. */
  readonly subStages = BUILD_SUB_STAGES;

  /**
   * Descriptor of the sub-stage currently shown.
   * Defensive: `activeBuildSubStage()` is range-guarded by the state service,
   * so this is always in range — but fail loud rather than hand the template
   * an `undefined` sub-stage if that invariant is ever broken.
   */
  readonly activeSubStageDef = computed(() => {
    const idx = this.state.activeBuildSubStage();
    /* v8 ignore next 3 -- defensive: activeBuildSubStage is range-guarded by AgentStudioStateService, so this branch is unreachable */
    if (idx < 0 || idx >= this.subStages.length) {
      throw new RangeError(`activeSubStageDef: active sub-stage index ${idx} is out of range`);
    }
    return this.subStages[idx];
  });

  /** The registry agent the current draft was cloned from (null until a clone succeeds). */
  readonly selectedSourceAgentId = signal<string | null>(null);
  /** Whether a clone request is in flight. */
  readonly cloning = signal(false);
  /** Message for the most recent failed clone, or null. */
  readonly cloneError = signal<string | null>(null);
  /** The cloned draft definition — the payload `saveAgent()` round-trips to the API. */
  readonly draftDefinition = signal<AgentDefinition | null>(null);

  /** Whether a save request is in flight. */
  readonly saving = signal(false);
  /** Message for the most recent failed save, or null. */
  readonly saveError = signal<string | null>(null);

  /** Whether 1.1 has a cloned draft ready to carry into Define/Configure. */
  readonly selectedAgentId = computed(() => this.selectedSourceAgentId());

  /** Whether the provisioning slide-out is open. */
  readonly provisionOpen = signal(false);

  /**
   * Clone the catalog's selected agent into a Stage-1 draft via
   * `AgentStudioApiService` (spec §3, Stage 1.1: "Duplicate & refine" — the
   * source registry agent is never mutated). This is the "select" step only —
   * advancing to Stage 2 is the shell's gated "Test this agent →" affordance,
   * unlocked once the draft is saved (§3, Stage 1.3).
   */
  onSelectAgent(agentId: string): void {
    if (this.cloning()) return;
    this.cloning.set(true);
    this.cloneError.set(null);
    this.api
      .cloneFromRegistry(agentId)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (definition) => {
          this.cloning.set(false);
          this.draftDefinition.set(definition);
          this.selectedSourceAgentId.set(agentId);
        },
        error: (err) => {
          this.cloning.set(false);
          this.cloneError.set(err?.error?.detail ?? 'Could not clone this agent — try again.');
        },
      });
  }

  /**
   * Save + register the current draft via `AgentStudioApiService`. On success,
   * the returned `agent_id` becomes the journey's `registryAgentId` — the id
   * the shell's "Test this agent →" gate and the rest of the journey read
   * (spec §2.4: "On Save the draft agent is registered and its registry id is
   * written to registryAgentId").
   *
   * Preconditions: none enforced — a missing draft or an in-flight save is a
   *   normal no-op (the Configure action row only renders once a draft exists).
   * Postconditions: on success, `state.registryAgentId()` is the new agent id
   *   and `saveError()` is null; on failure, `draftDefinition()` and the active
   *   sub-stage are unchanged and `saveError()` carries the surfaced message,
   *   so the user can retry without losing the draft or being bounced.
   */
  saveAgent(): void {
    const definition = this.draftDefinition();
    if (this.saving() || !definition) return;
    this.saving.set(true);
    this.saveError.set(null);
    const req: SaveAgentRequest = {
      name: definition.name,
      role: definition.role,
      description: definition.description,
      tags: definition.tags,
      tools: definition.tools,
      system_prompt: definition.system_prompt,
      input_schema: definition.input_schema,
      output_schema: definition.output_schema,
      states: definition.states,
    };
    this.api
      .saveAgent(req)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (response) => {
          this.saving.set(false);
          this.state.setRegistryAgentId(response.agent_id);
        },
        error: (err) => {
          this.saving.set(false);
          this.saveError.set(err?.error?.detail ?? 'Could not save this agent — try again.');
        },
      });
  }

  /** 1.1 Start → 1.2 Define. */
  continueToDefine(): void {
    this.state.advanceBuildSubStage();
  }

  /** 1.2 Define → 1.3 Configure. */
  continueToConfigure(): void {
    this.state.advanceBuildSubStage();
  }

  /** 1.3 Configure ◂ 1.2 Define — the sub-stepper's one backward affordance. */
  backToDefine(): void {
    this.state.backToDefine();
  }

  openProvision(): void {
    this.provisionOpen.set(true);
  }

  closeProvision(): void {
    this.provisionOpen.set(false);
  }
}
