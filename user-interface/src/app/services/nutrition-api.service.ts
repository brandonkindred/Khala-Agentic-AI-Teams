import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, EMPTY, of, throwError, timer } from 'rxjs';
import { expand, first, switchMap } from 'rxjs/operators';
import { environment } from '../../environments/environment';
import type {
  ClientProfile,
  FeedbackResponse,
  MealHistoryResponse,
  MealPlanResponse,
  NutritionChatMessage,
  NutritionChatRequest,
  NutritionChatResponse,
  NutritionHealthResponse,
  NutritionPlanResponse,
  NutritionProfileUpdateRequest,
} from '../models';

interface NutritionJobSubmission {
  job_id: string;
  status: string;
}

interface NutritionJobStatus<T> {
  job_id: string;
  status: string;
  result?: T | null;
  error?: string | null;
  not_found?: boolean;
}

const NUTRITION_POLL_INTERVAL_MS = 2000;

@Injectable({ providedIn: 'root' })
export class NutritionApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.nutritionApiUrl;

  healthCheck(): Observable<NutritionHealthResponse> {
    return this.http.get<NutritionHealthResponse>(`${this.baseUrl}/health`);
  }

  getProfile(clientId: string): Observable<ClientProfile> {
    return this.http.get<ClientProfile>(`${this.baseUrl}/profile/${encodeURIComponent(clientId)}`);
  }

  upsertProfile(clientId: string, body: NutritionProfileUpdateRequest): Observable<ClientProfile> {
    return this.http.put<ClientProfile>(`${this.baseUrl}/profile/${encodeURIComponent(clientId)}`, body);
  }

  /** Submit a nutrition plan job and poll until completed. */
  generateNutritionPlan(clientId: string): Observable<NutritionPlanResponse> {
    return this.http
      .post<NutritionJobSubmission>(`${this.baseUrl}/plan/nutrition`, { client_id: clientId })
      .pipe(switchMap((s) => this.pollJob<NutritionPlanResponse>(s.job_id)));
  }

  /** Submit a regenerate-nutrition-plan job and poll until completed. */
  regenerateNutritionPlan(clientId: string): Observable<NutritionPlanResponse> {
    return this.http
      .post<NutritionJobSubmission>(
        `${this.baseUrl}/plan/nutrition/${encodeURIComponent(clientId)}/regenerate`,
        {}
      )
      .pipe(switchMap((s) => this.pollJob<NutritionPlanResponse>(s.job_id)));
  }

  /** Submit a meal plan job and poll until completed. */
  generateMealPlan(
    clientId: string,
    periodDays: number,
    mealTypes: string[]
  ): Observable<MealPlanResponse> {
    return this.http
      .post<NutritionJobSubmission>(`${this.baseUrl}/plan/meals`, {
        client_id: clientId,
        period_days: periodDays,
        meal_types: mealTypes,
      })
      .pipe(switchMap((s) => this.pollJob<MealPlanResponse>(s.job_id)));
  }

  getJob<T>(jobId: string): Observable<NutritionJobStatus<T>> {
    return this.http.get<NutritionJobStatus<T>>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}`);
  }

  private pollJob<T>(jobId: string): Observable<T> {
    const poll$ = this.getJob<T>(jobId);
    return poll$.pipe(
      expand((job) =>
        job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled'
          ? EMPTY
          : timer(NUTRITION_POLL_INTERVAL_MS).pipe(switchMap(() => poll$))
      ),
      first((job) =>
        job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled'
      ),
      switchMap((job) =>
        job.status === 'completed' && job.result
          ? of(job.result)
          : throwError(() => new Error(job.error || `Nutrition job ${job.status}`))
      )
    );
  }

  submitFeedback(
    clientId: string,
    recommendationId: string,
    rating?: number,
    wouldMakeAgain?: boolean,
    notes?: string
  ): Observable<FeedbackResponse> {
    return this.http.post<FeedbackResponse>(`${this.baseUrl}/feedback`, {
      client_id: clientId,
      recommendation_id: recommendationId,
      rating,
      would_make_again: wouldMakeAgain,
      notes,
    });
  }

  getMealHistory(clientId: string): Observable<MealHistoryResponse> {
    return this.http.get<MealHistoryResponse>(`${this.baseUrl}/history/meals?client_id=${encodeURIComponent(clientId)}`);
  }

  sendChatMessage(request: NutritionChatRequest): Observable<NutritionChatResponse> {
    return this.http.post<NutritionChatResponse>(`${this.baseUrl}/chat`, request);
  }

  getChatHistory(clientId: string): Observable<{ client_id: string; messages: NutritionChatMessage[] }> {
    return this.http.get<{ client_id: string; messages: NutritionChatMessage[] }>(
      `${this.baseUrl}/chat/history/${encodeURIComponent(clientId)}`
    );
  }
}
