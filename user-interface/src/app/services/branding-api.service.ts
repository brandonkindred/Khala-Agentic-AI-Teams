import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  Brand,
  BrandingQuestion,
  BrandingSessionResponse,
  Client,
  CompetitiveSnapshot,
  CreateBrandRequest,
  CreateClientRequest,
  DesignAssetRequestResult,
  RunBrandRequest,
  RunBrandingTeamRequest,
  TeamOutput,
  UpdateBrandRequest,
  HealthResponse,
} from '../models';

@Injectable({ providedIn: 'root' })
export class BrandingApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.brandingApiUrl;

  listClients(): Observable<Client[]> {
    return this.http.get<Client[]>(`${this.baseUrl}/branding/clients`);
  }

  getClient(clientId: string): Observable<Client> {
    return this.http.get<Client>(`${this.baseUrl}/branding/clients/${clientId}`);
  }

  createClient(request: CreateClientRequest): Observable<Client> {
    return this.http.post<Client>(`${this.baseUrl}/branding/clients`, request);
  }

  listBrands(clientId: string): Observable<Brand[]> {
    return this.http.get<Brand[]>(`${this.baseUrl}/branding/clients/${clientId}/brands`);
  }

  getBrand(clientId: string, brandId: string): Observable<Brand> {
    return this.http.get<Brand>(`${this.baseUrl}/branding/clients/${clientId}/brands/${brandId}`);
  }

  createBrand(clientId: string, request: CreateBrandRequest): Observable<Brand> {
    return this.http.post<Brand>(`${this.baseUrl}/branding/clients/${clientId}/brands`, request);
  }

  updateBrand(clientId: string, brandId: string, request: UpdateBrandRequest): Observable<Brand> {
    return this.http.put<Brand>(`${this.baseUrl}/branding/clients/${clientId}/brands/${brandId}`, request);
  }

  runBrand(clientId: string, brandId: string, request?: RunBrandRequest): Observable<TeamOutput> {
    return this.http.post<TeamOutput>(
      `${this.baseUrl}/branding/clients/${clientId}/brands/${brandId}/run`,
      request ?? { human_approved: true }
    );
  }

  requestMarketResearch(clientId: string, brandId: string): Observable<CompetitiveSnapshot> {
    return this.http.post<CompetitiveSnapshot>(
      `${this.baseUrl}/branding/clients/${clientId}/brands/${brandId}/request-market-research`,
      {}
    );
  }

  requestDesignAssets(clientId: string, brandId: string): Observable<DesignAssetRequestResult> {
    return this.http.post<DesignAssetRequestResult>(
      `${this.baseUrl}/branding/clients/${clientId}/brands/${brandId}/request-design-assets`,
      {}
    );
  }

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
