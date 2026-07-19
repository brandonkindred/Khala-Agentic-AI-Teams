/** Format a fraction-of-100 value as a percentage string, e.g. `formatPct(12.34)` → `'12.3%'`. */
export function formatPct(value: number, decimals = 1): string {
  return value.toFixed(decimals) + '%';
}

/** Format a plain ratio value (Sharpe, profit factor, …) to a fixed number of decimals. */
export function formatRatio(value: number, decimals = 2): string {
  return value.toFixed(decimals);
}
