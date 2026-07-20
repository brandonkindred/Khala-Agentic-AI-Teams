/** Format a percentage value as a string, e.g. `formatPct(12.34)` → `'12.3%'`. */
export function formatPct(value: number, decimals = 1): string {
  return value.toFixed(decimals) + '%';
}

/** Format a plain ratio value (Sharpe, profit factor, …) to a fixed number of decimals. */
export function formatRatio(value: number, decimals = 2): string {
  return value.toFixed(decimals);
}

/** Format a USD amount with a leading $ and thousands separators, e.g. `formatUsd(150000)` → `'$150,000'`. */
export function formatUsd(value: number, decimals = 0): string {
  return '$' + value.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}
