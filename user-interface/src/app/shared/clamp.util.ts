/**
 * Clamp a numeric input to an integer within `[min, max]`. Non-finite input
 * (e.g. a cleared number field, which yields `NaN`) falls back to `min`
 * rather than propagating `NaN` into a request payload.
 *
 * Preconditions: `min <= max`, and both `min` and `max` are integers.
 * Postconditions: returns an integer `n` with `min <= n <= max`.
 */
export function clamp(value: number, min: number, max: number): number {
  const n = Number.isFinite(value) ? Math.floor(value) : min;
  return Math.max(min, Math.min(max, n));
}
