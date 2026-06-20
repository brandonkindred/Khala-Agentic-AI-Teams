import { describe, it, expect } from 'vitest';
import { globSync } from 'glob';
import { readFileSync } from 'node:fs';

/**
 * Static accessibility guard for component stylesheets.
 *
 * The Khala design system (`src/theme.scss`, `src/styles.scss`) ships a
 * high-contrast `--kh-*` token set and a global amber focus ring. This spec
 * stops component SCSS from drifting back into the two patterns that broke
 * accessibility before:
 *
 *   1. Hardcoded low-contrast TEXT colors (dark grays / opaque-black `rgba`)
 *      that are near-invisible on the dark theme. Text must use `--kh-text-*`
 *      or a semantic `--kh-{success,warning,error,info}` token instead.
 *   2. `outline: none` inside a `:focus` / `:focus-visible` block WITHOUT a
 *      replacement accent ring, which kills the visible keyboard-focus
 *      indicator. If a component drops the outline it must draw its own ring
 *      via `box-shadow: ... var(--kh-focus-ring | --kh-accent)`.
 *
 * Preconditions: run from the `user-interface` package root (vitest cwd).
 * Postconditions: fails if any non-allowlisted `src/app/**` component SCSS
 * contains a banned pattern; fails if an allowlisted file is already clean
 * (forcing it off the burndown list) or no longer exists (no stale entries).
 *
 * This spec reads source files only — no Angular/app code executes — so it is
 * coverage-neutral and does not affect the 90% line-coverage gate.
 */

// Banned hardcoded TEXT colors. Anchored on a `color:` declaration that is not
// part of `background-color` / `border-color` / `outline-color` / etc.
const BANNED_HEX = '#(?:5{3}|6{3}|7{3}|8{3}|9{3}|a{3}|b{3}|c{3})(?:[0-9a-f]{3})?|#(?:8b949e|484f58|6e7681|71717a)';
const BANNED_TEXT_COLOR = new RegExp(
  String.raw`(?<![-\w])color:\s*(?:${BANNED_HEX}|rgba\(\s*0\s*,\s*0\s*,\s*0)`,
  'i',
);

// A `:focus` / `:focus-visible` rule body (flat block, no nested braces).
const FOCUS_BLOCK = /&?:focus(?:-visible)?\s*\{([^}]*)\}/gi;
// An accent focus ring that legitimises dropping the outline.
const RING_SHADOW = /box-shadow:[^;]*var\(\s*--kh-(?:focus-ring|accent)\s*\)/i;

/**
 * Burndown allowlist — component SCSS files known to still violate the rules
 * above. The accessibility remediation removes entries here as each file is
 * migrated onto the `--kh-*` tokens; the guard then enforces them forever.
 * When this array is empty the whole UI is clean. DO NOT add new entries to
 * silence a failure on new code — fix the code instead.
 */
const BURNDOWN = new Set<string>([
  'src/app/components/accessibility-design-system/accessibility-design-system.component.scss',
  'src/app/components/accessibility-report/accessibility-report.component.scss',
  'src/app/components/agent-console/agent-catalog/agent-catalog.component.scss',
  'src/app/components/agent-console/agent-run-history/agent-run-history.component.scss',
  'src/app/components/agent-console/backlog-tab/backlog-tab.component.scss',
  'src/app/components/agent-test-chat/agent-test-chat.component.scss',
  'src/app/components/agentic-team-dashboard/agentic-team-dashboard.component.scss',
  'src/app/components/branding-chat/branding-chat.component.scss',
  'src/app/components/branding-dashboard/branding-dashboard.component.scss',
  'src/app/components/coding-team-monitor/coding-team-monitor.component.scss',
  'src/app/components/flow-step-editor/flow-step-editor.component.scss',
  'src/app/components/investment-chat/investment-chat.component.scss',
  'src/app/components/investment-profile-form/investment-profile-form.component.scss',
  'src/app/components/investment-promotion/investment-promotion.component.scss',
  'src/app/components/investment-strategy/investment-strategy.component.scss',
  'src/app/components/investment-workflow/investment-workflow.component.scss',
  'src/app/components/nutrition-dashboard/nutrition-dashboard.component.scss',
  'src/app/components/pa-calendar/pa-calendar.component.scss',
  'src/app/components/pa-chat/pa-chat.component.scss',
  'src/app/components/pa-deals/pa-deals.component.scss',
  'src/app/components/pa-documents/pa-documents.component.scss',
  'src/app/components/pa-reservations/pa-reservations.component.scss',
  'src/app/components/pa-tasks/pa-tasks.component.scss',
  'src/app/components/persona-chat/persona-chat.component.scss',
  'src/app/components/pipeline-test-runner/pipeline-test-runner.component.scss',
  'src/app/components/process-designer-chat/process-designer-chat.component.scss',
  'src/app/components/product-analysis-job-status/product-analysis-job-status.component.scss',
  'src/app/components/road-trip-planning-dashboard/road-trip-planning-dashboard.component.scss',
  'src/app/components/sales-dashboard/sales-dashboard.component.scss',
  'src/app/components/start-from-spec-form/start-from-spec-form.component.scss',
  'src/app/components/startup-advisor-dashboard/startup-advisor-dashboard.component.scss',
  'src/app/components/strategy-lab/strategy-lab.component.scss',
]);

/** Returns the list of offenses in a stylesheet (empty when clean). */
function findOffenses(source: string): string[] {
  const offenses: string[] = [];

  source.split('\n').forEach((line, i) => {
    if (line.trimStart().startsWith('//')) return;
    if (BANNED_TEXT_COLOR.test(line)) {
      offenses.push(`L${i + 1}: hardcoded low-contrast text color — use a --kh-text-* / semantic token`);
    }
  });

  for (const match of source.matchAll(FOCUS_BLOCK)) {
    const body = match[1];
    if (/outline:\s*(?:none|0)\b/i.test(body) && !RING_SHADOW.test(body)) {
      offenses.push('focus block drops `outline` with no accent ring — keep the outline or add box-shadow var(--kh-focus-ring)');
    }
  }

  return offenses;
}

const files = globSync('src/app/**/*.scss').map((f) => f.split('\\').join('/')).sort();

describe('SCSS accessibility guard', () => {
  it('finds component stylesheets to scan', () => {
    expect(files.length).toBeGreaterThan(0);
  });

  it.each(files)('%s uses design-system tokens for text & focus', (file) => {
    const offenses = findOffenses(readFileSync(file, 'utf8'));
    if (BURNDOWN.has(file)) {
      // Still on the burndown: it must genuinely still offend, otherwise it has
      // been fixed and should be removed from BURNDOWN so the guard enforces it.
      expect(offenses.length, `${file} is now clean — remove it from BURNDOWN`).toBeGreaterThan(0);
    } else {
      expect(offenses, `${file}:\n  ${offenses.join('\n  ')}`).toHaveLength(0);
    }
  });

  it('has no stale burndown entries (every listed file still exists)', () => {
    const present = new Set(files);
    const stale = [...BURNDOWN].filter((f) => !present.has(f));
    expect(stale, `stale BURNDOWN entries:\n  ${stale.join('\n  ')}`).toHaveLength(0);
  });
});
