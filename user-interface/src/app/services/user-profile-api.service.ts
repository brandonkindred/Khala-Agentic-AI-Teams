import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
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

  /** GET /api/user-profile — current (default) profile. */
  getProfile(): Observable<UserProfile> {
    return this.http.get<UserProfile>(this.baseUrl);
  }

  /** PUT /api/user-profile — update profile fields. */
  updateProfile(body: UserProfileUpdate): Observable<UserProfile> {
    return this.http.put<UserProfile>(this.baseUrl, body);
  }

  /**
   * GET /api/user-profile/overview — profile + associations + integrations in a
   * single response, so the profile page loads in one round-trip.
   */
  getOverview(): Observable<ProfileOverview> {
    return this.http.get<ProfileOverview>(`${this.baseUrl}/overview`);
  }
}
