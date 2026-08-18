import { ChangeDetectionStrategy, Component, OnInit, inject } from '@angular/core';
import { STAGE_INDEX } from '../../../models/agent-studio.model';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { PersonaTestAuditPanelComponent } from '../persona-test-audit-panel/persona-test-audit-panel.component';

/**
 * Studio host for the persona audit panel (nested `/agent-studio/persona-run/:runId`).
 *
 * Preconditions: provided inside a Studio shell so `AgentStudioStateService` resolves.
 * Postconditions: after init, `activeStage()` is Personas (3). The audit panel is
 *   mounted with Studio back inputs; `:runId` is left to the panel's `ActivatedRoute`.
 * Invariants: this wrapper does not poll, fetch artifacts, or own a second Back control.
 */
@Component({
  selector: 'app-agent-studio-persona-audit',
  standalone: true,
  imports: [PersonaTestAuditPanelComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './agent-studio-persona-audit.component.html',
  styleUrl: './agent-studio-persona-audit.component.scss',
})
export class AgentStudioPersonaAuditComponent implements OnInit {
  private readonly state = inject(AgentStudioStateService);

  ngOnInit(): void {
    this.state.navigateToStage(STAGE_INDEX.personas);
  }
}
