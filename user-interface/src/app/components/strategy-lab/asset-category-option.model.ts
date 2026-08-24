import { ASSET_CLASS_ICONS } from './strategy-lab.formatters';

/**
 * One selectable asset category, as rendered by both `StrategyLabComponent`'s
 * category state and `GenerateStrategiesDialogComponent`'s toggle group.
 * Feature-local (not a `models/` API-mirroring type) so it lives outside
 * both components rather than being owned by either — keeping the
 * parent/dialog import direction one-way (parent → dialog only).
 */
export interface AssetCategoryOption {
  value: string;
  label: string;
  icon: string;
}

/** Title-case an asset-category value for display (e.g. 'stocks' → 'Stocks'). */
function categoryLabel(value: string): string {
  return value.length ? value.charAt(0).toUpperCase() + value.slice(1) : value;
}

/** Build selector options from category values, deriving label + Material icon. */
export function buildCategoryOptions(values: string[]): AssetCategoryOption[] {
  return values.map((value) => ({
    value,
    label: categoryLabel(value),
    icon: ASSET_CLASS_ICONS[value] ?? 'category',
  }));
}
