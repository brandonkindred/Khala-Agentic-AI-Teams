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
   * server; on failure the previous values are retained (no error surfaced —
   * this only feeds decorative chrome).
   */
  refresh(): void {
    // getProfile() emits once and completes (HttpClient), so no unsubscribe is
    // needed for this root singleton.
    this.api.getProfile().subscribe({
      next: (profile) => this.set(profile.display_name ?? '', profile.preferences?.['avatar_color']),
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
    this._displayName.set(displayName ?? '');
    this._avatarColorKey.set(resolveAvatarColor(colorKey).key);
  }
}
