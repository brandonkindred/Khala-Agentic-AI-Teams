import { axe } from 'vitest-axe';
import { expect } from 'vitest';

/**
 * Shared vitest-axe options for component a11y specs.
 *
 * `color-contrast` is disabled because jsdom can't paint, so axe can't compute
 * composited colours (and hangs on HTMLCanvasElement.getContext). Contrast is
 * enforced separately by src/styles/scss-contrast-guard.spec.ts + browser axe
 * DevTools.
 */
export const axeOptions = { rules: { 'color-contrast': { enabled: false } } };

/**
 * Runs axe (with `color-contrast` disabled) against `host` and asserts there are
 * no accessibility violations.
 *
 * Uses the `expect` singleton that `src/test-setup.mjs` extends with the
 * vitest-axe matchers, so `toHaveNoViolations` is registered. Pass `extraRules`
 * to disable additional rules on top of `color-contrast` for a component that
 * has a documented, unfixable exception (e.g. an isolated fragment whose
 * required ARIA parent lives in another component).
 *
 * Preconditions: `host` is a rendered element (assert a real node is present
 *   first so the audit isn't vacuous).
 * Postconditions: resolves when axe reports zero violations; the surrounding
 *   test fails otherwise.
 */
export async function expectNoAxeViolations(
  host: Element,
  extraRules: Record<string, { enabled: boolean }> = {},
): Promise<void> {
  const results = await axe(host, { rules: { ...axeOptions.rules, ...extraRules } });
  expect(results).toHaveNoViolations();
}
