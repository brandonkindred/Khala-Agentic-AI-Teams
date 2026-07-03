import { Injectable, computed, inject, signal } from '@angular/core';
import { UserProfileApiService } from './user-profile-api.service';
import { resolveAvatarColor } from '../shared/avatar/avatar-colors';

/**
 * Root-provided cache of the current user's identity for chrome that renders
 * it outside the profile page (today: the sidenav footer avatar).
 *
 * Invariants: `displayName` is the trimmed stored name (or ''), and
 * `avatarColorKey` is always a valid palette key (unknown/absent → default).
 * The store never throws on a failed refresh — it just keeps its last values.
 */
@Injectable({ providedIn: 'root' })
export class UserProfileStore {
  private readonly api = inject(UserProfileApiService);

  private readonly _displayName = signal('');
  private readonly _avatarColorKey = signal(resolveAvatarColor(undefined).key);

  /**
   * Monotonic write counter. Every `set()` bumps it; an in-flight `refresh()`
   * captures it and only applies its (older) response when no `set()` has run
   * since — so a slow boot-time refresh can't overwrite a fresh save.
   */
  private writeSeq = 0;

  /** Current display name, or '' when unknown/unset. */
  readonly displayName = this._displayName.asReadonly();
  /** Current avatar color palette key (always valid). */
  readonly avatarColorKey = this._avatarColorKey.asReadonly();
  /** True once a name is known, so consumers can show initials vs a generic icon. */
  readonly hasIdentity = computed(() => this._displayName().trim().length > 0);

  /**
   * Fetch the profile and update the cached identity.
   *
   * Preconditions: none.
   * Postconditions: on success `displayName`/`avatarColorKey` reflect the
   * server UNLESS a `set()` ran while the request was in flight (a fresh save
   * wins over a stale boot-time fetch); on failure the previous values are
   * retained. The request is `silent`, so a profile error never surfaces the
   * global toast over an unrelated page — this only feeds decorative chrome.
   */
  refresh(): void {
    const seq = this.writeSeq;
    // getProfile() emits once and completes (HttpClient), so no unsubscribe is
    // needed for this root singleton.
    this.api.getProfile({ silent: true }).subscribe({
      next: (profile) => {
        if (seq !== this.writeSeq) return; // a set() superseded this fetch
        this.set(profile.display_name ?? '', profile.preferences?.['avatar_color']);
      },
      error: () => {
        /* decorative-only: keep last-known identity */
      },
    });
  }

  /**
   * Update the cached identity directly (e.g. after the profile page saves),
   * avoiding a round-trip.
   *
   * Preconditions: none — `colorKey` is untrusted and normalized.
   * Postconditions: the signals reflect the given name and resolved color.
   */
  set(displayName: string, colorKey: unknown): void {
    this.writeSeq++;
    this._displayName.set(displayName ?? '');
    this._avatarColorKey.set(resolveAvatarColor(colorKey).key);
  }
}
