import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable, EMPTY, of, throwError, timer } from 'rxjs';
import { expand, first, switchMap } from 'rxjs/operators';
import { environment } from '../../environments/environment';
import type {
  AssistantRequest,
  AssistantResponse,
  UserProfile,
  ProfileUpdateRequest,
  ProfileUpdateResponse,
  TaskList,
  TaskItem,
  AddTasksFromTextRequest,
  AddTasksFromTextResponse,
  EventFromTextRequest,
  EventFromTextResponse,
  WishlistItem,
  AddWishlistRequest,
  DealSearchRequest,
  DealSearchResponse,
  Reservation,
  CreateReservationRequest,
  ReservationFromTextRequest,
  ReservationResult,
  GeneratedDocument,
  DocumentGenerateRequest,
  HealthResponse,
} from '../models';

interface AssistantJobSubmitResponse {
  job_id: string;
  status: string;
  message?: string;
}

interface AssistantJobStatusResponse {
  job_id: string;
  user_id: string;
  status: string;
  progress?: number;
  status_text?: string;
  response?: AssistantResponse | null;
  error?: string | null;
}

const ASSISTANT_POLL_INTERVAL_MS = 1000;

/**
 * Service for Personal Assistant API endpoints.
 * Base URL from environment.personalAssistantApiUrl (default port 8015).
 */
@Injectable({ providedIn: 'root' })
export class PersonalAssistantApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.personalAssistantApiUrl;

  /**
   * Submit an assistant job and poll until it terminates.
   * POST /assistant/jobs?user_id=... → GET /assistant/jobs/{job_id}.
   */
  sendMessage(userId: string, request: AssistantRequest): Observable<AssistantResponse> {
    const params = new HttpParams().set('user_id', userId);
    return this.http
      .post<AssistantJobSubmitResponse>(`${this.baseUrl}/assistant/jobs`, request, { params })
      .pipe(switchMap((submit) => this.pollAssistantJob(submit.job_id)));
  }

  private pollAssistantJob(jobId: string): Observable<AssistantResponse> {
    const statusUrl = `${this.baseUrl}/assistant/jobs/${jobId}`;
    const poll$ = this.http.get<AssistantJobStatusResponse>(statusUrl);
    return poll$.pipe(
      expand((job) =>
        job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled'
          ? EMPTY
          : timer(ASSISTANT_POLL_INTERVAL_MS).pipe(switchMap(() => poll$))
      ),
      first((job) =>
        job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled'
      ),
      switchMap((job) =>
        job.status === 'completed' && job.response
          ? of(job.response)
          : throwError(() => new Error(job.error || `Assistant job ${job.status}`))
      )
    );
  }

  /**
   * GET /users/{userId}/profile
   * Get the user's profile.
   */
  getProfile(userId: string): Observable<UserProfile> {
    return this.http.get<UserProfile>(`${this.baseUrl}/users/${userId}/profile`);
  }

  /**
   * POST /users/{userId}/profile
   * Update the user's profile.
   */
  updateProfile(userId: string, request: ProfileUpdateRequest): Observable<ProfileUpdateResponse> {
    return this.http.post<ProfileUpdateResponse>(
      `${this.baseUrl}/users/${userId}/profile`,
      request
    );
  }

  /**
   * GET /users/{userId}/tasks
   * Get all task lists for the user.
   */
  getTasks(userId: string): Observable<TaskList[]> {
    return this.http.get<TaskList[]>(`${this.baseUrl}/users/${userId}/tasks`);
  }

  /**
   * POST /users/{userId}/tasks/from-text
   * Add tasks from natural language text.
   */
  addTasksFromText(userId: string, request: AddTasksFromTextRequest): Observable<AddTasksFromTextResponse> {
    return this.http.post<AddTasksFromTextResponse>(
      `${this.baseUrl}/users/${userId}/tasks/from-text`,
      request
    );
  }

  /**
   * PATCH /users/{userId}/tasks/{listId}/items/{itemId}
   * Update a task item (e.g., mark as completed).
   */
  updateTaskItem(
    userId: string,
    listId: string,
    itemId: string,
    updates: Partial<TaskItem>
  ): Observable<TaskItem> {
    return this.http.patch<TaskItem>(
      `${this.baseUrl}/users/${userId}/tasks/${listId}/items/${itemId}`,
      updates
    );
  }

  /**
   * POST /users/{userId}/calendar/events/from-text
   * Create calendar event from natural language.
   */
  createEventFromText(userId: string, request: EventFromTextRequest): Observable<EventFromTextResponse> {
    return this.http.post<EventFromTextResponse>(
      `${this.baseUrl}/users/${userId}/calendar/events/from-text`,
      request
    );
  }

  /**
   * GET /users/{userId}/deals/wishlist
   * Get the user's wishlist.
   */
  getWishlist(userId: string): Observable<WishlistItem[]> {
    return this.http.get<WishlistItem[]>(`${this.baseUrl}/users/${userId}/deals/wishlist`);
  }

  /**
   * POST /users/{userId}/deals/wishlist
   * Add item to wishlist.
   */
  addToWishlist(userId: string, request: AddWishlistRequest): Observable<WishlistItem> {
    return this.http.post<WishlistItem>(
      `${this.baseUrl}/users/${userId}/deals/wishlist`,
      request
    );
  }

  /**
   * DELETE /users/{userId}/deals/wishlist/{itemId}
   * Remove item from wishlist.
   */
  removeFromWishlist(userId: string, itemId: string): Observable<void> {
    return this.http.delete<void>(
      `${this.baseUrl}/users/${userId}/deals/wishlist/${itemId}`
    );
  }

  /**
   * POST /users/{userId}/deals/search
   * Search for deals.
   */
  searchDeals(userId: string, request: DealSearchRequest): Observable<DealSearchResponse> {
    return this.http.post<DealSearchResponse>(
      `${this.baseUrl}/users/${userId}/deals/search`,
      request
    );
  }

  /**
   * GET /users/{userId}/reservations
   * Get all reservations for the user.
   */
  getReservations(userId: string): Observable<Reservation[]> {
    return this.http.get<Reservation[]>(`${this.baseUrl}/users/${userId}/reservations`);
  }

  /**
   * POST /users/{userId}/reservations
   * Create a new reservation.
   */
  createReservation(userId: string, request: CreateReservationRequest): Observable<ReservationResult> {
    return this.http.post<ReservationResult>(
      `${this.baseUrl}/users/${userId}/reservations`,
      request
    );
  }

  /**
   * POST /users/{userId}/reservations/from-text
   * Create reservation from natural language.
   */
  createReservationFromText(userId: string, request: ReservationFromTextRequest): Observable<ReservationResult> {
    return this.http.post<ReservationResult>(
      `${this.baseUrl}/users/${userId}/reservations/from-text`,
      request
    );
  }

  /**
   * GET /users/{userId}/documents
   * Get all generated documents for the user.
   */
  getDocuments(userId: string): Observable<GeneratedDocument[]> {
    return this.http.get<GeneratedDocument[]>(`${this.baseUrl}/users/${userId}/documents`);
  }

  /**
   * POST /users/{userId}/documents
   * Generate a new document.
   */
  generateDocument(userId: string, request: DocumentGenerateRequest): Observable<GeneratedDocument> {
    return this.http.post<GeneratedDocument>(
      `${this.baseUrl}/users/${userId}/documents`,
      request
    );
  }

  /**
   * GET /users/{userId}/documents/{docId}
   * Get a specific document.
   */
  getDocument(userId: string, docId: string): Observable<GeneratedDocument> {
    return this.http.get<GeneratedDocument>(
      `${this.baseUrl}/users/${userId}/documents/${docId}`
    );
  }

  /**
   * GET /health
   * Health check for the Personal Assistant API.
   */
  health(): Observable<HealthResponse> {
    return this.http.get<HealthResponse>(`${this.baseUrl}/health`);
  }
}
