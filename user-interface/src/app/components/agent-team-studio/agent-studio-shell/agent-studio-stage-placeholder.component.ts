import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { MatIconModule } from '@angular/material/icon';
import { AgentStudioHandoffState } from '../../../models/agent-studio.model';

/**
 * Temporary stub for an Agent Studio stage. Each real stage (Build / Test /
 * Compose / Personas) replaces this in a later increment; for the scaffold it
 * shows the stage title and blurb plus a read-out of the live handoff state, so
 * the navigation spine is demonstrable end-to-end.
 */
@Component({
  selector: 'app-agent-studio-stage-placeholder',
  standalone: true,
  imports: [MatIconModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './agent-studio-stage-placeholder.component.html',
  styleUrl: './agent-studio-stage-placeholder.component.scss',
})
export class AgentStudioStagePlaceholderComponent {
  /** Stepper label of the stage being stubbed. */
  readonly title = input.required<string>();
  /** One-line description of the stage. */
  readonly blurb = input.required<string>();
  /** Material icon for the stage. */
  readonly icon = input.required<string>();
  /** Live handoff state, shown as a small read-out. */
  readonly handoff = input.required<AgentStudioHandoffState>();

  /**
   * Handoff entries as `[label, value]` pairs for the template. The keys are
   * listed explicitly here so this debug read-out stays a deliberate, ordered
   * view; the real stages won't render a raw handoff dump, so dynamic
   * derivation from the model isn't warranted for this throwaway placeholder.
   */
  readonly entries = computed<readonly (readonly [string, string])[]>(() => {
    const h = this.handoff();
    return [
      ['registryAgentId', h.registryAgentId ?? '—'],
      ['teamId', h.teamId ?? '—'],
      ['processId', h.processId ?? '—'],
      ['personaId', h.personaId ?? '—'],
      ['draftAgentId', h.draftAgentId ?? '—'],
    ];
  });
}
