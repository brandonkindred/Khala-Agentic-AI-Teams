import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpContext } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import { SKIP_ERROR_NOTIFY } from '../core/error-handler.interceptor';
import type {
  ProfileOverview,
  UserProfile,
  UserProfileUpdate,
} from '../models/user-profile.model';

/**
 * Service for the User Profile API (/api/user-profile).
 * Base URL from environment.userProfileApiUrl.
 */
@Injectable({ providedIn: 'root' })
export class UserProfileApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.userProfileApiUrl;

  /**
   * GET /api/user-profile — current (default) profile.
   *
   * Preconditions: none (the backend auto-creates the default profile on first read).
   * Postconditions: the observable emits the current `UserProfile`, or errors with
   * the `HttpErrorResponse` (e.g. 503 when profile storage is unavailable). When
   * `options.silent` is true the global error toast is suppressed for this
   * request (the caller handles failure itself — e.g. the footer avatar fetch).
   */
  getProfile(options?: { silent?: boolean }): Observable<UserProfile> {
    const context = new HttpContext();
    if (options?.silent) context.set(SKIP_ERROR_NOTIFY, true);
    return this.http.get<UserProfile>(this.baseUrl, { context });
  }

  /**
   * PUT /api/user-profile — update profile fields.
   *
   * Preconditions: `body` conforms to `UserProfileUpdate`. Omitted fields are left
   * unchanged server-side. A present scalar field (display_name/email/bio) is
   * written verbatim, so send the full desired value, not a fragment.
   * Postconditions: the observable emits the updated `UserProfile`, or errors with
   * the `HttpErrorResponse`. A present `preferences` dict is MERGED key-by-key
   * into the stored object server-side (top-level keys overwrite; keys absent
   * from the update survive) — send only the keys you own. There is no
   * key-deletion path, but a `null` value is stored and read as absent (the
   * sanctioned way to reset a preference).
   */
  updateProfile(body: UserProfileUpdate): Observable<UserProfile> {
    return this.http.put<UserProfile>(this.baseUrl, body);
  }

  /**
   * GET /api/user-profile/overview — profile + associations + integrations in a
   * single response, so the profile page loads in one round-trip.
   *
   * Preconditions: none.
   * Postconditions: the observable emits a `ProfileOverview` whose `profile`,
   * `associations`, and `integrations` are all present, or errors with the
   * `HttpErrorResponse`.
   */
  getOverview(): Observable<ProfileOverview> {
    return this.http.get<ProfileOverview>(`${this.baseUrl}/overview`);
  }
}
