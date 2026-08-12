import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { STUDIO_STAGES } from '../../../models/agent-studio.model';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { AgentStudioBuildAgentComponent } from './agent-studio-build-agent.component';
import { AgentStudioComposeTeamComponent } from './agent-studio-compose-team.component';
import { AgentStudioPersonaComponent } from './agent-studio-persona.component';
import { AgentStudioStagePlaceholderComponent } from './agent-studio-stage-placeholder.component';
import { AgentStudioTestAgentComponent } from './agent-studio-test-agent.component';

/**
 * Default `/agent-studio` child: the four-stage switch that used to live in the
 * shell template.
 *
 * Preconditions: `AgentStudioStateService` is provided by an ancestor (the shell).
 * Postconditions: the template renders exactly one of the four stage components
 *   (or the defensive placeholder) matching `state.activeStage()`.
 */
@Component({
  selector: 'app-agent-studio-stage-host',
  standalone: true,
  imports: [
    AgentStudioBuildAgentComponent,
    AgentStudioComposeTeamComponent,
    AgentStudioPersonaComponent,
    AgentStudioStagePlaceholderComponent,
    AgentStudioTestAgentComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './agent-studio-stage-host.component.html',
})
export class AgentStudioStageHostComponent {
  readonly state = inject(AgentStudioStateService);
  readonly stages = STUDIO_STAGES;

  readonly activeStageDef = computed(() => {
    const idx = this.state.activeStage();
    /* v8 ignore next 3 -- defensive: activeStage is range-guarded by AgentStudioStateService */
    if (idx < 0 || idx >= this.stages.length) {
      throw new RangeError(`activeStageDef: active stage index ${idx} is out of range`);
    }
    return this.stages[idx];
  });
}
