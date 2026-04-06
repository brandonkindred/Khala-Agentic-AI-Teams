import { Component, EventEmitter, Input, Output } from '@angular/core';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';

/**
 * Contextual empty state with description and clickable example prompts.
 *
 * Replaces the generic "No X yet" pattern across dashboards with
 * rich context about what the team does and quick-start examples.
 */
@Component({
  selector: 'app-empty-state',
  standalone: true,
  imports: [MatIconModule, MatButtonModule],
  templateUrl: './empty-state.component.html',
  styleUrl: './empty-state.component.scss',
})
export class EmptyStateComponent {
  /** Material icon name displayed prominently. */
  @Input() icon = 'inbox';
  @Input() title = 'Nothing here yet';
  /** Describes what the team/feature does. */
  @Input() description = '';
  /** Example prompts the user can click to quick-start. */
  @Input() examples: string[] = [];
  /** Emitted when the user clicks an example prompt. */
  @Output() exampleClick = new EventEmitter<string>();
}
