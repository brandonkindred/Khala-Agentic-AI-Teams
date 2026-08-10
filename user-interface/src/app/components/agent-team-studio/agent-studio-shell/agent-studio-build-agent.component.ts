import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { A11yModule } from '@angular/cdk/a11y';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { BUILD_SUB_STAGES } from '../../../models/agent-studio.model';
import { AgentCatalogComponent } from '../agent-console/agent-catalog/agent-catalog.component';
import { AgentProvisionSlideOutComponent } from '../agent-provision-slide-out/agent-provision-slide-out.component';
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
 * drawer's run action) records it as the journey's `registryAgentId`; an
 * explicit "Continue to Define →" then advances the sub-stepper. **1.2
 * Define** and **1.3 Configure** are scaffolded with the shared stage
 * placeholder (their real content — the build assistant and the full
 * anatomy review — is a later increment); each carries its own forward
 * chrome, and Configure carries the sub-stepper's one backward affordance
 * (`◂ back to Define`, spec §3, Stage 1). The main-stepper's gated
 * "Test this agent →" (Stage 1 → Stage 2) is unaffected by the sub-stepper.
 *
 * Provisioning is folded into this stage (spec §3, Stage 1): a "Provision an
 * agent" affordance opens `AgentProvisionSlideOutComponent`, a thin,
 * route-agnostic wrapper (spec §4.1's Stage 1 adaptation caveat) around the
 * same provisioning chat used by the full `AgentProvisioningDashboardComponent`
 * — without that component's `DashboardShellComponent` page chrome or its
 * `ActivatedRoute`-based `?jobId=` deep link, neither of which apply on this
 * route. The full dashboard is untouched and still used as-is in Agent
 * Console's own "Provisioning & Environments" tab. The slide-out content is
 * self-contained, so it is only mounted while the panel is open. The
 * slide-out itself is a proper modal: a CDK focus trap with auto-capture
 * moves focus into the panel, keeps Tab cycling inside it, and restores focus
 * to the trigger on close; Escape dismisses it.
 */
@Component({
  selector: 'app-agent-studio-build-agent',
  standalone: true,
  imports: [
    A11yModule,
    MatButtonModule,
    MatIconModule,
    AgentCatalogComponent,
    AgentProvisionSlideOutComponent,
    AgentStudioStagePlaceholderComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './agent-studio-build-agent.component.html',
  styleUrl: './agent-studio-build-agent.component.scss',
})
export class AgentStudioBuildAgentComponent {
  readonly state = inject(AgentStudioStateService);

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

  /** The agent picked for the journey (null until one is selected). */
  readonly selectedAgentId = computed(() => this.state.registryAgentId());

  /** Whether the provisioning slide-out is open. */
  readonly provisionOpen = signal(false);

  /**
   * Record the catalog's selected agent as the journey's `registryAgentId`.
   * This is the "select" step only — advancing to Stage 2 is the shell's gated
   * "Test this agent →" affordance (spec §3, Stage 1 handoff).
   */
  onSelectAgent(agentId: string): void {
    this.state.setRegistryAgentId(agentId);
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
