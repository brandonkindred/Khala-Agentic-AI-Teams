/**
 * "N <noun> shown" text for a live-region announcement of a filtered list's
 * result count (e.g. "1 repository shown", "3 repositories shown").
 *
 * Preconditions: `count` is a non-negative integer; `singular`/`plural` are the
 * noun's singular and plural forms, with no trailing "shown".
 * Postconditions: returns `"1 <singular> shown"` when `count` is 1;
 * `"<count> <plural> shown"` otherwise (including when `count` is 0). Pure —
 * no side effects.
 */
export function resultCountAnnouncement(count: number, singular: string, plural: string): string {
  return count === 1 ? `1 ${singular} shown` : `${count} ${plural} shown`;
}
