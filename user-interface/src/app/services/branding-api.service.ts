import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  BrandingQuestion,
  BrandingSessionResponse,
  RunBrandingTeamRequest,
  HealthResponse,
} from '../models';

@Injectable({ providedIn: 'root' })
export class BrandingApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.brandingApiUrl;

  createSession(request: RunBrandingTeamRequest): Observable<BrandingSessionResponse> {
    return this.http.post<BrandingSessionResponse>(`${this.baseUrl}/branding/sessions`, request);
  }

  getSession(sessionId: string): Observable<BrandingSessionResponse> {
    return this.http.get<BrandingSessionResponse>(`${this.baseUrl}/branding/sessions/${sessionId}`);
  }

  getOpenQuestions(sessionId: string): Observable<BrandingQuestion[]> {
    return this.http.get<BrandingQuestion[]>(`${this.baseUrl}/branding/sessions/${sessionId}/questions`);
  }

  answerQuestion(sessionId: string, questionId: string, answer: string): Observable<BrandingSessionResponse> {
    return this.http.post<BrandingSessionResponse>(
      `${this.baseUrl}/branding/sessions/${sessionId}/questions/${questionId}/answer`,
      { answer }
    );
  }

  health(): Observable<HealthResponse> {
    return this.http.get<HealthResponse>(`${this.baseUrl}/health`);
  }
}
