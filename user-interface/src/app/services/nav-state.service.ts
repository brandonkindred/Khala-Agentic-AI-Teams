import { inject, Injectable, signal, computed } from '@angular/core';
import { Router, NavigationEnd } from '@angular/router';
import { filter } from 'rxjs/operators';
import { ALL_NAV_ITEMS, NavItem, findGroupForRoute } from '../models/navigation.model';

const FAVORITES_KEY = 'kh-nav-favorites';
const COLLAPSED_KEY = 'kh-nav-collapsed';

/**
 * Manages sidebar navigation state: collapsed groups and pinned favorites.
 * Persists to localStorage so state survives page reloads.
 * Uses Angular signals for reactive UI updates.
 */
@Injectable({ providedIn: 'root' })
export class NavStateService {
  /** Set of NavItem IDs that are pinned as favorites. */
  readonly favorites = signal<Set<string>>(this.loadSet(FAVORITES_KEY));

  /** Set of NavGroup keys that are currently collapsed. */
  readonly collapsedGroups = signal<Set<string>>(this.loadSet(COLLAPSED_KEY));

  /** Resolved NavItem objects for all favorites (preserves insertion order). */
  readonly favoriteItems = computed<NavItem[]>(() => {
    const ids = this.favorites();
    return ALL_NAV_ITEMS.filter(item => ids.has(item.id));
  });

  private readonly router = inject(Router);

  constructor() {
    this.router.events.pipe(
      filter((e): e is NavigationEnd => e instanceof NavigationEnd),
    ).subscribe(e => this.expandGroupForRoute(e.urlAfterRedirects));
  }

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

  // ── Collapsed Groups ─────────────────────────────────────────────────────

  isCollapsed(groupKey: string): boolean {
    return this.collapsedGroups().has(groupKey);
  }

  toggleGroup(groupKey: string): void {
    this.collapsedGroups.update(set => {
      const next = new Set(set);
      if (next.has(groupKey)) {
        next.delete(groupKey);
      } else {
        next.add(groupKey);
      }
      return next;
    });
    this.persistSet(COLLAPSED_KEY, this.collapsedGroups());
  }

  /**
   * Auto-expand the nav group that contains the active route.
   * Called on every NavigationEnd.
   */
  expandGroupForRoute(url: string): void {
    const group = findGroupForRoute(url);
    if (group && this.collapsedGroups().has(group.key)) {
      this.collapsedGroups.update(set => {
        const next = new Set(set);
        next.delete(group.key);
        return next;
      });
      this.persistSet(COLLAPSED_KEY, this.collapsedGroups());
    }
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
