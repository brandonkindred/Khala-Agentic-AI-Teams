# Accessibility

## Overview

The UI follows WCAG 2.2-oriented practices and Angular Material accessibility guidelines.

## ARIA

- **ARIA attribute form** – Prefer a plain attribute for a constant value (`aria-label="Remove goal"`), and `[attr.aria-*]` for a value computed at runtime (`[attr.aria-label]="'Cancel ' + job.label"`) — that is the binding form Angular supports for ARIA attributes; the plain `[aria-*]` form is not used anywhere in this codebase. This is a preference for new code, not a rule to enforce retroactively: both forms are correct, many existing templates bind a constant through `[attr.aria-*]`, and converting between the two in either direction is a no-op refactor rather than a fix.
- **aria-label** – Buttons, icons, and controls have descriptive labels.
- **aria-current** – Navigation items use `aria-current="page"` for the active route.
- **aria-live** – Dynamic content uses a live region for screen-reader announcements. Status and progress updates are `polite`, so they queue behind whatever the user is reading. A failure the user must act on interrupts instead — the global error toast opens `assertive` deliberately, because polite announcements of a failure are routinely missed. In components, express the interrupting case as `role="alert"` (the repo's prevailing form), which already implies `aria-live="assertive"`. Pairing a role with its implied `aria-live` is redundant but harmless, and this codebase does it deliberately for `role="status" aria-live="polite"` — so it is a matter of local consistency. Reserve a bare `aria-live="assertive"` for a region that must interrupt but is not an alert. A region that carries both routine status and occasional failures is a judgement call — splitting the failures into their own `role="alert"` region beats downgrading everything to `polite`.
- **role** – Main content has `role="main"`; alerts use `role="alert"`.

## Keyboard Navigation

- All interactive elements are focusable and operable via keyboard.
- Tab order follows the visual layout.
- Skip link ("Skip to main content") is the first focusable element and appears on focus.
- Material components provide built-in keyboard support (tabs, dialogs, etc.).

## Focus Management

- Main content area has `tabindex="-1"` for programmatic focus (e.g. after skip link).
- No keyboard traps; focus moves logically through the interface.

## Screen Readers

- Form fields are associated with labels via `mat-label` and `aria-label`.
- Error messages use `aria-describedby` where applicable.
- Loading and status changes are announced via `aria-live` regions.

## Material Components

- Angular Material components are used with their default accessibility behavior.
- Icon-only buttons receive `aria-label`.
- Form validation errors are exposed to assistive technologies.

## Testing

- Run Lighthouse (Chrome DevTools) for accessibility audits.
- Use axe DevTools for automated checks.
- Test with a screen reader (e.g. NVDA, VoiceOver).

## Reviewing a team's pages

For a structured audit of one team's routed pages — WCAG 2.2 AA lenses, the fourteen
per-page states and their dispositions, and a finding format that names a concrete
mechanism — use the reusable prompt at
[`docs/prompts/ux-accessibility-review.md`](../../docs/prompts/ux-accessibility-review.md).
It records what the jsdom unit harness (`expectNoAxeViolations`) and the SCSS contrast
lint do and do not guard, so a review spends its attention on what they structurally
cannot see. Those blind spots are not the same as Lighthouse's or axe DevTools': a
browser tool measures the contrast and geometry jsdom cannot, so the prompt complements
the tooling above rather than replacing it — run both.
