import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
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

  generateNutritionPlan(clientId: string): Observable<NutritionPlanResponse> {
    return this.http.post<NutritionPlanResponse>(`${this.baseUrl}/plan/nutrition`, { client_id: clientId });
  }

  generateMealPlan(clientId: string, periodDays: number, mealTypes: string[]): Observable<MealPlanResponse> {
    return this.http.post<MealPlanResponse>(`${this.baseUrl}/plan/meals`, {
      client_id: clientId,
      period_days: periodDays,
      meal_types: mealTypes,
    });
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
