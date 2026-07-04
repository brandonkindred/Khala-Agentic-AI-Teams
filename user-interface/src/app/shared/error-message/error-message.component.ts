import { Component, Input } from '@angular/core';
import { InlineBannerComponent } from '../inline-banner/inline-banner.component';

/**
 * Reusable inline error message display.
 *
 * A thin preset over {@link InlineBannerComponent}: the card-layout error
 * variant. Kept as a distinct selector (`app-error-message`) so existing call
 * sites and the title/message defaults stay unchanged, while the actual
 * rendering (colors, icon, live region) lives in the shared banner.
 */
@Component({
  selector: 'app-error-message',
  standalone: true,
  imports: [InlineBannerComponent],
  template: `<app-inline-banner variant="error" layout="card" icon="error" [title]="title" [message]="message" />`,
})
export class ErrorMessageComponent {
  /** Error message to display. */
  @Input() message = 'An error occurred.';

  /** Optional title. Default "Error". */
  @Input() title = 'Error';
}
