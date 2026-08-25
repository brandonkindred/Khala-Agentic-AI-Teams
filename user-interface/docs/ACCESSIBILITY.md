# Accessibility

## Overview

The UI follows WCAG 2.2-oriented practices and Angular Material accessibility guidelines.

## ARIA

- **attr. prefix** – All ARIA attributes use `[attr.aria-*]` in templates (e.g. `[attr.aria-label]`, `[attr.aria-live]`) to avoid Angular binding issues.
- **aria-label** – Buttons, icons, and controls have descriptive labels.
- **aria-current** – Navigation items use `aria-current="page"` for the active route.
- **aria-live** – Dynamic content (errors, status) uses `aria-live="polite"` for screen reader announcements.
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
It records what the automated checks here do and do not guard, so a review spends its
attention on what Lighthouse and axe structurally cannot see.
