import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output } from '@angular/core';
import { A11yModule } from '@angular/cdk/a11y';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';

/**
 * Shared slide-out shell for Agent Studio's fixed right-hand overlay pattern
 * (scrim + focus-trapped panel + Escape/scrim/close-button dismissal). Purely
 * presentational — the host owns open/close state and decides when to render
 * this component's projected content.
 *
 * Preconditions: `heading` is a non-empty, human-readable string; callers
 *   render this component only while they intend `open` to reflect their own
 *   boolean/signal state (there is no internal debounce or animation, so
 *   toggling `open` renders/destroys the panel and its projected content
 *   synchronously on the next change-detection pass).
 * Postconditions: while `open` is true, exactly one scrim + one `role="dialog"`
 *   `aria-modal="true"` panel is rendered, focus is trapped inside the panel
 *   (`cdkTrapFocus` with auto-capture), and clicking the scrim, clicking the
 *   close button, or pressing Escape while focus is inside the panel each emit
 *   exactly one `closeRequested` event and do not themselves change `open` —
 *   the host must set its own state to false in response. While `open` is
 *   false, nothing is rendered and the projected content is destroyed (not
 *   hidden), matching Stage 1's original behavior of only mounting its
 *   slide-out content while visible.
 */
@Component({
  selector: 'app-agent-studio-slide-out',
  standalone: true,
  imports: [A11yModule, MatButtonModule, MatIconModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './agent-studio-slide-out.component.html',
  styleUrl: './agent-studio-slide-out.component.scss',
})
export class AgentStudioSlideOutComponent {
  /** Whether the slide-out is rendered. The host owns this state. */
  @Input({ required: true }) open = false;

  /** Visible `<h2>` title and the panel's `aria-label`. */
  @Input({ required: true }) heading = '';

  /** aria-label for the close icon button; defaults to `Close ${heading}` when omitted. */
  @Input() closeButtonLabel?: string;

  /** CSS `width` for the panel. Defaults to Stage 1's original slide-out width. */
  @Input() panelWidth = 'min(560px, 92vw)';

  /** Emitted once from a scrim click, close-button click, or Escape. Never emitted by any other path. */
  @Output() readonly closeRequested = new EventEmitter<void>();

  /** Preconditions: none. Postconditions: emits exactly one `closeRequested`; does not mutate `open`. */
  onScrimClick(): void {
    this.closeRequested.emit();
  }

  /** Preconditions: none. Postconditions: emits exactly one `closeRequested`; does not mutate `open`. */
  onEscape(): void {
    this.closeRequested.emit();
  }

  /** Preconditions: none. Postconditions: emits exactly one `closeRequested`; does not mutate `open`. */
  onCloseButtonClick(): void {
    this.closeRequested.emit();
  }
}
