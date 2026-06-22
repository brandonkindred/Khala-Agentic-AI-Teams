import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  AssociationList,
  ProfileIntegration,
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
   * GET /api/user-profile/associations — artifacts linked to the profile,
   * optionally filtered by artifact_type.
   */
  getAssociations(artifactType?: string): Observable<AssociationList> {
    let params = new HttpParams();
    if (artifactType) {
      params = params.set('artifact_type', artifactType);
    }
    return this.http.get<AssociationList>(`${this.baseUrl}/associations`, { params });
  }

  /** GET /api/user-profile/integrations — integration status pass-through. */
  getIntegrations(): Observable<ProfileIntegration[]> {
    return this.http.get<ProfileIntegration[]>(`${this.baseUrl}/integrations`);
  }

  /**
   * GET /api/user-profile/overview — profile + associations + integrations in a
   * single response, so the profile page loads in one round-trip.
   */
  getOverview(): Observable<ProfileOverview> {
    return this.http.get<ProfileOverview>(`${this.baseUrl}/overview`);
  }
}
