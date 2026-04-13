import { HttpClient } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  FounderJobSummary,
  PersonaInfo,
  PersonaTestRun,
  PersonaTestRunDetail,
  PersonaDecision,
  PersonaChatHistory,
  RunArtifacts,
} from '../models';

@Injectable({ providedIn: 'root' })
export class PersonaTestingApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.personaTestingApiUrl;

  getPersonas(): Observable<{ personas: PersonaInfo[] }> {
    return this.http.get<{ personas: PersonaInfo[] }>(`${this.baseUrl}/personas`);
  }

  startTest(): Observable<{ run_id: string; status: string; message: string }> {
    return this.http.post<{ run_id: string; status: string; message: string }>(
      `${this.baseUrl}/start`,
      {},
    );
  }

  getRuns(): Observable<{ runs: PersonaTestRun[] }> {
    return this.http.get<{ runs: PersonaTestRun[] }>(`${this.baseUrl}/runs`);
  }

  getRunStatus(runId: string): Observable<PersonaTestRunDetail> {
    return this.http.get<PersonaTestRunDetail>(`${this.baseUrl}/status/${runId}`);
  }

  getDecisions(runId: string): Observable<PersonaDecision[]> {
    return this.http.get<PersonaDecision[]>(`${this.baseUrl}/decisions/${runId}`);
  }

  getRunArtifacts(runId: string): Observable<RunArtifacts> {
    return this.http.get<RunArtifacts>(`${this.baseUrl}/runs/${runId}/artifacts`);
  }

  listJobs(runningOnly: boolean): Observable<{ jobs: FounderJobSummary[] }> {
    const url = runningOnly
      ? `${this.baseUrl}/jobs?running_only=true`
      : `${this.baseUrl}/jobs`;
    return this.http.get<{ jobs: FounderJobSummary[] }>(url);
  }

  cancelJob(jobId: string): Observable<unknown> {
    return this.http.post(`${this.baseUrl}/job/${encodeURIComponent(jobId)}/cancel`, {});
  }

  deleteJob(jobId: string): Observable<unknown> {
    return this.http.delete(`${this.baseUrl}/job/${encodeURIComponent(jobId)}`);
  }

  getChatHistory(runId: string, sinceId?: number): Observable<PersonaChatHistory> {
    const params = sinceId ? `?since_id=${sinceId}` : '';
    return this.http.get<PersonaChatHistory>(
      `${this.baseUrl}/runs/${encodeURIComponent(runId)}/chat${params}`,
    );
  }

  sendChatMessage(runId: string, message: string): Observable<PersonaChatHistory> {
    return this.http.post<PersonaChatHistory>(
      `${this.baseUrl}/runs/${encodeURIComponent(runId)}/chat`,
      { message },
    );
  }
}
