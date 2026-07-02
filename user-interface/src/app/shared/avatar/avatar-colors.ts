/**
 * Named avatar color palette shared by the initials avatar and its pickers.
 *
 * Colors are persisted by *key* (e.g. "amber") in the free-form profile
 * `preferences` JSONB and resolved to `--kh-*` theme tokens at render time,
 * so a theme retune never invalidates stored profiles.
 *
 * Invariants: `AVATAR_COLOR_OPTIONS` is non-empty, keys are unique, and
 * `DEFAULT_AVATAR_COLOR` is always one of its keys.
 */

/** One selectable avatar color: a stable key, a human label, and a theme token. */
export interface AvatarColorOption {
  /** Stable identifier persisted in profile preferences. */
  key: string;
  /** Human-readable name (used as the swatch aria-label). */
  label: string;
  /** CSS custom property name (including leading `--`) supplying the fill. */
  cssVar: string;
}

/** The selectable palette, mapped onto bright semantic theme tokens. */
export const AVATAR_COLOR_OPTIONS: readonly AvatarColorOption[] = [
  { key: 'amber', label: 'Amber', cssVar: '--kh-accent' },
  { key: 'green', label: 'Green', cssVar: '--kh-success' },
  { key: 'blue', label: 'Blue', cssVar: '--kh-info' },
  { key: 'red', label: 'Red', cssVar: '--kh-error' },
];

/** Fallback color key used when a stored value is missing or unrecognized. */
export const DEFAULT_AVATAR_COLOR = 'amber';

/**
 * Resolve a stored color key to its palette option.
 *
 * Preconditions: none — `key` is typed `unknown` because it comes from the
 * free-form `preferences` JSONB, which callers must not trust.
 * Postconditions: returns the matching option when `key` is a known palette
 * key; otherwise returns the `DEFAULT_AVATAR_COLOR` option. Never throws.
 */
export function resolveAvatarColor(key: unknown): AvatarColorOption {
  const match = AVATAR_COLOR_OPTIONS.find((option) => option.key === key);
  // The default key is a palette invariant, so the non-null assertion is safe.
  return match ?? AVATAR_COLOR_OPTIONS.find((option) => option.key === DEFAULT_AVATAR_COLOR)!;
}
