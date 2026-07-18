import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

/**
 * Guards the Khala design system's semantic color tokens in `src/theme.scss`
 * against silently re-colliding. `--kh-warning` and `--kh-accent` previously
 * shared the identical hex value, making "running" and "pending/waiting"
 * status badges indistinguishable by color alone (WCAG 1.4.1).
 *
 * Preconditions: `src/theme.scss` declares `--kh-accent` and `--kh-warning`,
 *   each as a hex color literal terminated by `;` on its own declaration line.
 * Postconditions: fails if either token isn't found or isn't hex-shaped;
 *   fails if the two tokens resolve to the same hex value (case-insensitive).
 */

const THEME_PATH = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'theme.scss');
const theme = readFileSync(THEME_PATH, 'utf8');

/**
 * Hex value of a `--kh-<name>: <hex>;` token declaration.
 *
 * Preconditions: `name` excludes the `--` prefix and trailing `:`.
 * Postconditions: returns the matched hex literal, or undefined if the token
 *   isn't declared. The colon anchors the match to the exact property name,
 *   so `kh-accent` doesn't also match a longer token like `kh-accent-hover`.
 */
function tokenHex(source: string, name: string): string | undefined {
  return source.match(new RegExp(String.raw`--${name}:\s*(#[0-9a-fA-F]{3,8})`))?.[1];
}

describe('theme token guard', () => {
  it('declares --kh-accent and --kh-warning as hex colors', () => {
    expect(tokenHex(theme, 'kh-accent')).toMatch(/^#[0-9a-fA-F]{3,8}$/);
    expect(tokenHex(theme, 'kh-warning')).toMatch(/^#[0-9a-fA-F]{3,8}$/);
  });

  it('--kh-warning does not share a hex value with --kh-accent', () => {
    const accent = tokenHex(theme, 'kh-accent');
    const warning = tokenHex(theme, 'kh-warning');
    expect(warning?.toLowerCase()).not.toEqual(accent?.toLowerCase());
  });
});
