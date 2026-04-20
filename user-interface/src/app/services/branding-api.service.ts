import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, EMPTY, of, throwError, timer } from 'rxjs';
import { expand, first, switchMap } from 'rxjs/operators';
import { environment } from '../../environments/environment';
import type {
  Brand,
  BrandingQuestion,
  BrandingSessionResponse,
  Client,
  CompetitiveSnapshot,
  ConversationStateResponse,
  CreateBrandRequest,
  CreateClientRequest,
  CreateConversationRequest,
  DesignAssetRequestResult,
  RunBrandRequest,
  RunBrandingTeamRequest,
  BrandingTeamOutput,
  UpdateBrandRequest,
  HealthResponse,
} from '../models';

interface BrandJobSubmission {
  job_id: string;
  status: string;
}

interface BrandJobStatus {
  job_id: string;
  status: string;
  current_phase?: string | null;
  result?: BrandingTeamOutput | null;
  error?: string | null;
}

const BRANDING_POLL_INTERVAL_MS = 2000;

@Injectable({ providedIn: 'root' })
export class BrandingApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.brandingApiUrl;

  listClients(): Observable<Client[]> {
    return this.http.get<Client[]>(`${this.baseUrl}/clients`);
  }

  getClient(clientId: string): Observable<Client> {
    return this.http.get<Client>(`${this.baseUrl}/clients/${clientId}`);
  }

  createClient(request: CreateClientRequest): Observable<Client> {
    return this.http.post<Client>(`${this.baseUrl}/clients`, request);
  }

  listBrands(clientId: string): Observable<Brand[]> {
    return this.http.get<Brand[]>(`${this.baseUrl}/clients/${clientId}/brands`);
  }

  getBrand(clientId: string, brandId: string): Observable<Brand> {
    return this.http.get<Brand>(`${this.baseUrl}/clients/${clientId}/brands/${brandId}`);
  }

  createBrand(clientId: string, request: CreateBrandRequest): Observable<Brand> {
    return this.http.post<Brand>(`${this.baseUrl}/clients/${clientId}/brands`, request);
  }

  updateBrand(clientId: string, brandId: string, request: UpdateBrandRequest): Observable<Brand> {
    return this.http.put<Brand>(`${this.baseUrl}/clients/${clientId}/brands/${brandId}`, request);
  }

  /**
   * Submit a branding run job and poll until it completes. Emits the final
   * `BrandingTeamOutput`; errors on `failed` / `cancelled`.
   */
  runBrand(
    clientId: string,
    brandId: string,
    request?: RunBrandRequest
  ): Observable<BrandingTeamOutput> {
    return this.submitRun(clientId, brandId, request).pipe(
      switchMap((submission) => this.pollJob(submission.job_id))
    );
  }

  /** Submit a branding run job; returns immediately with a `job_id`. */
  submitRun(
    clientId: string,
    brandId: string,
    request?: RunBrandRequest
  ): Observable<BrandJobSubmission> {
    return this.http.post<BrandJobSubmission>(
      `${this.baseUrl}/clients/${clientId}/brands/${brandId}/run`,
      request ?? { human_approved: true }
    );
  }

  /** Single status poll for a branding job. */
  getJobStatus(jobId: string): Observable<BrandJobStatus> {
    return this.http.get<BrandJobStatus>(`${this.baseUrl}/branding/status/${jobId}`);
  }

  private pollJob(jobId: string): Observable<BrandingTeamOutput> {
    const poll$ = this.getJobStatus(jobId);
    return poll$.pipe(
      expand((job) =>
        job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled'
          ? EMPTY
          : timer(BRANDING_POLL_INTERVAL_MS).pipe(switchMap(() => poll$))
      ),
      first((job) =>
        job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled'
      ),
      switchMap((job) =>
        job.status === 'completed' && job.result
          ? of(job.result)
          : throwError(() => new Error(job.error || `Branding job ${job.status}`))
      )
    );
  }

  requestMarketResearch(clientId: string, brandId: string): Observable<CompetitiveSnapshot> {
    return this.http.post<CompetitiveSnapshot>(
      `${this.baseUrl}/clients/${clientId}/brands/${brandId}/request-market-research`,
      {}
    );
  }

  requestDesignAssets(clientId: string, brandId: string): Observable<DesignAssetRequestResult> {
    return this.http.post<DesignAssetRequestResult>(
      `${this.baseUrl}/clients/${clientId}/brands/${brandId}/request-design-assets`,
      {}
    );
  }

  createSession(request: RunBrandingTeamRequest): Observable<BrandingSessionResponse> {
    return this.http.post<BrandingSessionResponse>(`${this.baseUrl}/sessions`, request);
  }

  getSession(sessionId: string): Observable<BrandingSessionResponse> {
    return this.http.get<BrandingSessionResponse>(`${this.baseUrl}/sessions/${sessionId}`);
  }

  getOpenQuestions(sessionId: string): Observable<BrandingQuestion[]> {
    return this.http.get<BrandingQuestion[]>(`${this.baseUrl}/sessions/${sessionId}/questions`);
  }

  answerQuestion(sessionId: string, questionId: string, answer: string): Observable<BrandingSessionResponse> {
    return this.http.post<BrandingSessionResponse>(
      `${this.baseUrl}/sessions/${sessionId}/questions/${questionId}/answer`,
      { answer }
    );
  }

  createConversation(initialMessage?: string | null): Observable<ConversationStateResponse> {
    const body: CreateConversationRequest = initialMessage != null ? { initial_message: initialMessage } : {};
    return this.http.post<ConversationStateResponse>(`${this.baseUrl}/conversations`, body);
  }

  sendConversationMessage(conversationId: string, message: string): Observable<ConversationStateResponse> {
    return this.http.post<ConversationStateResponse>(
      `${this.baseUrl}/conversations/${conversationId}/messages`,
      { message }
    );
  }

  getConversation(conversationId: string): Observable<ConversationStateResponse> {
    return this.http.get<ConversationStateResponse>(`${this.baseUrl}/conversations/${conversationId}`);
  }

  getBrandConversation(clientId: string, brandId: string): Observable<ConversationStateResponse> {
    return this.http.get<ConversationStateResponse>(
      `${this.baseUrl}/clients/${clientId}/brands/${brandId}/conversation`
    );
  }

  health(): Observable<HealthResponse> {
    return this.http.get<HealthResponse>(`${this.baseUrl}/health`);
  }
}
