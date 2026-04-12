import { Injectable, signal, computed } from '@angular/core';
import { ALL_NAV_ITEMS, NavItem } from '../models/navigation.model';

const FAVORITES_KEY = 'kh-nav-favorites';

/**
 * Manages sidebar navigation state: pinned favorites.
 * Persists to localStorage so state survives page reloads.
 * Uses Angular signals for reactive UI updates.
 */
@Injectable({ providedIn: 'root' })
export class NavStateService {
  /** Set of NavItem IDs that are pinned as favorites. */
  readonly favorites = signal<Set<string>>(this.loadSet(FAVORITES_KEY));

  /** Resolved NavItem objects for all favorites (preserves insertion order). */
  readonly favoriteItems = computed<NavItem[]>(() => {
    const ids = this.favorites();
    return ALL_NAV_ITEMS.filter(item => ids.has(item.id));
  });

  // ── Favorites ────────────────────────────────────────────────────────────

  isFavorite(id: string): boolean {
    return this.favorites().has(id);
  }

  toggleFavorite(id: string): void {
    this.favorites.update(set => {
      const next = new Set(set);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
    this.persistSet(FAVORITES_KEY, this.favorites());
  }

  // ── Persistence ──────────────────────────────────────────────────────────

  private loadSet(key: string): Set<string> {
    try {
      const raw = localStorage.getItem(key);
      if (raw) {
        const arr = JSON.parse(raw);
        if (Array.isArray(arr)) {
          return new Set(arr.filter((v): v is string => typeof v === 'string'));
        }
      }
    } catch {
      // Corrupted data — start fresh.
    }
    return new Set();
  }

  private persistSet(key: string, set: Set<string>): void {
    try {
      localStorage.setItem(key, JSON.stringify([...set]));
    } catch {
      // localStorage full or unavailable — silently ignore.
    }
  }
}
