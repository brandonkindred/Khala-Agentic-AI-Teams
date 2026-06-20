import { describe, it, expect } from 'vitest';
import { globSync } from 'glob';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

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
 *   2. An `outline`-suppressing declaration (`none` / `0` / `0px` …) inside a
 *      `:focus` / `:focus-visible` block WITHOUT a replacement accent ring,
 *      which kills the visible keyboard-focus indicator. If a component drops
 *      the outline it must draw its own ring via
 *      `box-shadow: ... var(--kh-focus-ring | --kh-accent)`.
 *
 * Paths are resolved relative to this file, so the spec is independent of the
 * vitest working directory.
 * Postconditions: fails if any non-allowlisted `src/app/**` component SCSS
 * contains a banned pattern; fails if an allowlisted file is already clean
 * (forcing it off the burndown list) or no longer exists (no stale entries).
 *
 * This spec reads source files only — no Angular/app code executes — so it is
 * coverage-neutral and does not affect the 90% line-coverage gate.
 */

// Both regexes anchor on a `color:` declaration that is NOT part of
// `background-color` / `border-color` / `outline-color` / etc. (the negative
// lookbehind rejects a preceding `-` or word char).
//
// Two distinct offenses, because the remediation rationale differs by shade on
// this dark theme:
//   - Dark / mid grays + opaque-black rgba: genuinely low contrast as text on
//     the dark surfaces (near-invisible).
//   - Light grays (#888–#ccc): readable on dark, but still bypass the
//     `--kh-text-*` tokens, so they're banned for design-system consistency —
//     NOT labelled "low contrast", which would be factually wrong here.
const LOW_CONTRAST_HEX = '#(?:5{3}|6{3}|7{3})(?:[0-9a-f]{3})?|#(?:484f58|6e7681|71717a)';
const NON_TOKEN_GRAY_HEX = '#(?:8{3}|9{3}|a{3}|b{3}|c{3})(?:[0-9a-f]{3})?|#8b949e';
const LOW_CONTRAST_TEXT = new RegExp(
  String.raw`(?<![-\w])color:\s*(?:${LOW_CONTRAST_HEX}|rgba\(\s*0\s*,\s*0\s*,\s*0)`,
  'i',
);
const NON_TOKEN_GRAY_TEXT = new RegExp(String.raw`(?<![-\w])color:\s*(?:${NON_TOKEN_GRAY_HEX})`, 'i');

// Selector segment that targets a focus state — matches `:focus` and
// `:focus-visible` but not `:focus-within`.
const FOCUS_SELECTOR = /:focus-visible\b|:focus(?![-\w])/i;
// An `outline` declaration that removes the ring: `none`, or zero with/without
// a unit (`0`, `0px`, `0rem`, `0%`), terminated by `;`, `!important`, `}` or EOL.
// `0(?![.\d])` avoids matching a real thin outline like `0.5px`.
const OUTLINE_SUPPRESSED = /outline:\s*(?:none|0(?![.\d])(?:px|r?em|%)?)\s*(?:;|!|\}|$)/i;
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

/** Strips CSS block comments so they can't masquerade as selectors or decls. */
function stripBlockComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '');
}

/**
 * Returns the brace-balanced body of every rule whose selector targets a focus
 * state, regardless of where `:focus`/`:focus-visible` sits in a grouped
 * selector list and regardless of nesting. Handles cases the old flat-`[^}]*`
 * regex missed, e.g. `&:focus-visible, &:hover { … }` and nested child rules.
 */
function focusBlockBodies(source: string): string[] {
  const bodies: string[] = [];
  for (let i = 0; i < source.length; i++) {
    if (source[i] !== '{') continue;
    // Selector = text back to the previous block/declaration delimiter.
    let s = i - 1;
    while (s >= 0 && !'{};'.includes(source[s])) s--;
    if (!FOCUS_SELECTOR.test(source.slice(s + 1, i))) continue;
    // Capture this block's brace-balanced body.
    let depth = 1;
    let j = i + 1;
    for (; j < source.length && depth > 0; j++) {
      if (source[j] === '{') depth++;
      else if (source[j] === '}') depth--;
    }
    bodies.push(source.slice(i + 1, j - 1));
  }
  return bodies;
}

/** Returns the list of offenses in a stylesheet (empty when clean). */
function findOffenses(source: string): string[] {
  const offenses: string[] = [];

  source.split('\n').forEach((line, i) => {
    if (line.trimStart().startsWith('//')) return;
    if (LOW_CONTRAST_TEXT.test(line)) {
      offenses.push(`L${i + 1}: low-contrast hardcoded text color on the dark theme — use a --kh-text-* / semantic token`);
    } else if (NON_TOKEN_GRAY_TEXT.test(line)) {
      offenses.push(`L${i + 1}: hardcoded gray text color bypasses the --kh-text-* tokens — use a --kh-text-* token`);
    }
  });

  for (const body of focusBlockBodies(stripBlockComments(source))) {
    // Only the block's OWN declarations — drop nested rules so a child's
    // `outline: none` (or a child's ring) doesn't taint the parent's verdict.
    let own = body;
    let prev: string;
    do {
      prev = own;
      own = own.replace(/\{[^{}]*\}/g, '');
    } while (own !== prev);

    if (OUTLINE_SUPPRESSED.test(own) && !RING_SHADOW.test(own)) {
      offenses.push('focus state removes the outline with no accent ring — keep the outline or add box-shadow var(--kh-focus-ring)');
    }
  }

  return offenses;
}

// Resolve everything relative to this spec (…/src/styles/), so the guard is
// independent of the vitest working directory.
const PKG_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const files = globSync('src/app/**/*.scss', { cwd: PKG_ROOT }).map((f) => f.split('\\').join('/')).sort();

describe('SCSS accessibility guard', () => {
  it('finds component stylesheets to scan', () => {
    expect(files.length).toBeGreaterThan(0);
  });

  it.each(files)('%s uses design-system tokens for text & focus', (file) => {
    const offenses = findOffenses(readFileSync(resolve(PKG_ROOT, file), 'utf8'));
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

// Unit tests for the detector itself — these lock in the corner cases the
// file-level scan can't exercise on demand.
describe('findOffenses detector', () => {
  it('flags a focus state that drops the outline, even in a reordered group', () => {
    expect(findOffenses('.x { &:focus-visible, &:hover { outline: none; } }')).toHaveLength(1);
    expect(findOffenses('.x { &:hover, &:focus-visible { outline: none; } }')).toHaveLength(1);
  });

  it('allows dropping the outline when an accent box-shadow ring replaces it', () => {
    expect(findOffenses('.x:focus-visible { outline: none; box-shadow: 0 0 0 2px var(--kh-focus-ring); }')).toHaveLength(0);
    expect(findOffenses('.x:focus-visible { outline: none; box-shadow: inset 0 0 0 2px var(--kh-accent); }')).toHaveLength(0);
  });

  it('flags a decorative (non-ring) shadow that does not stand in for the outline', () => {
    expect(findOffenses('.x:focus-visible { outline: none; box-shadow: 0 6px 16px rgba(0,0,0,0.1); }')).toHaveLength(1);
  });

  it('matches outline suppression with or without a unit, but not a real thin outline', () => {
    expect(findOffenses('.x:focus-visible { outline: 0; }')).toHaveLength(1);
    expect(findOffenses('.x:focus-visible { outline: 0px; }')).toHaveLength(1);
    expect(findOffenses('.x:focus-visible { outline: 0.5px solid red; }')).toHaveLength(0);
  });

  it('ignores :focus-within and an outline:none in a nested child of a focus block', () => {
    expect(findOffenses('.x:focus-within { outline: none; }')).toHaveLength(0);
    expect(findOffenses('.x:focus-visible { box-shadow: 0 0 0 2px var(--kh-accent); .child { outline: none; } }')).toHaveLength(0);
  });

  it('labels dark grays as low-contrast and light grays as non-token', () => {
    expect(findOffenses('.x { color: #555; }')[0]).toMatch(/low-contrast/);
    expect(findOffenses('.x { color: rgba(0, 0, 0, 0.6); }')[0]).toMatch(/low-contrast/);
    expect(findOffenses('.x { color: #ccc; }')[0]).toMatch(/bypasses the --kh-text-\* tokens/);
  });

  it('does not flag non-text color properties or token usage', () => {
    expect(findOffenses('.x { background-color: #555; border-color: #ccc; }')).toHaveLength(0);
    expect(findOffenses('.x { color: var(--kh-text-secondary); }')).toHaveLength(0);
  });
});
