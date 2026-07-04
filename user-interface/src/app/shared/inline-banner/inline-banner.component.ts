import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { MatIconModule } from '@angular/material/icon';

/** Severity of a banner; drives its color, default icon, and live-region role. */
export type InlineBannerVariant = 'error' | 'warning' | 'success' | 'info';

/** Default Material icon shown for each variant when no `icon` override is given. */
const DEFAULT_ICONS: Record<InlineBannerVariant, string> = {
  error: 'error_outline',
  warning: 'warning_amber',
  success: 'check_circle',
  info: 'info',
};

/**
 * Shared inline banner for persistent / blocking states (load failures,
 * validation errors, actionable warnings, contextual info).
 *
 * This is the single home for the app's persistent-feedback convention:
 * transient confirmations (saved, copied, sent) belong in a snackbar
 * (`NotificationService.saved`), never here.
 *
 * Accessibility invariant: the assertive/polite live region (`role="alert"` for
 * error/warning, `role="status"` for success/info) wraps the **message only**.
 * Interactive controls are projected via the default `<ng-content>` into a
 * sibling region outside the live region — some assistive tech does not expose
 * controls nested inside an assertive live region.
 *
 * Preconditions: `variant` is one of the four documented values (the template
 * default keeps it valid); `message` and/or projected `[banner-message]`
 * content supply the human-readable text.
 * Postconditions: renders exactly one `.kh-banner--{variant}` element whose
 * color comes from `--kh-{variant}` theme tokens; the message text is the only
 * content inside the `[role]` live region.
 */
@Component({
  selector: 'app-inline-banner',
  standalone: true,
  imports: [MatIconModule],
  templateUrl: './inline-banner.component.html',
  styleUrl: './inline-banner.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class InlineBannerComponent {
  /** Severity of the banner. */
  readonly variant = input<InlineBannerVariant>('error');

  /** Plain-text message rendered inside the live region. */
  readonly message = input('');

  /** Optional Material icon name; falls back to the per-variant default. */
  readonly icon = input<string | undefined>(undefined);

  /**
   * Optional live-region override, decoupling announcement urgency from the
   * visual severity. `null` (default) derives it from `variant`; `'assertive'`
   * forces `role="alert"`, `'polite'` forces `role="status"`, and `'off'`
   * suppresses the live region entirely (for static, always-present text a
   * screen reader should not re-announce on render).
   */
  readonly live = input<'assertive' | 'polite' | 'off' | null>(null);

  /**
   * Icon shown, resolved from the `icon` override or the variant default.
   *
   * Preconditions: `variant()` is one of the four documented severities (the
   * `input` default guarantees this).
   * Postconditions: returns `icon()` when the caller set it; otherwise the
   * non-empty per-variant default Material icon name. Never returns undefined.
   */
  readonly resolvedIcon = computed(() => this.icon() ?? DEFAULT_ICONS[this.variant()]);

  /**
   * Live-region role for the message element.
   *
   * Preconditions: none.
   * Postconditions: when `live()` is set, maps `'assertive'`→`'alert'`,
   * `'polite'`→`'status'`, `'off'`→`null` (no live region). Otherwise derives
   * from severity: assertive `'alert'` for error/warning (the user must notice
   * a failure now), polite `'status'` for success/info. A `null` result means
   * the template renders no `role` attribute.
   */
  readonly liveRole = computed<'alert' | 'status' | null>(() => {
    switch (this.live()) {
      case 'assertive':
        return 'alert';
      case 'polite':
        return 'status';
      case 'off':
        return null;
      default: {
        const v = this.variant();
        return v === 'error' || v === 'warning' ? 'alert' : 'status';
      }
    }
  });
}
