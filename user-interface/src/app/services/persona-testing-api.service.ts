import { HttpClient } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  CreatePersonaRequest,
  FounderJobSummary,
  PersonaInfo,
  PersonaTestRun,
  PersonaTestRunDetail,
  PersonaDecision,
  PersonaChatHistory,
  RunArtifacts,
  StartTestRequest,
  TestableTeam,
  UpdatePersonaRequest,
} from '../models';

@Injectable({ providedIn: 'root' })
export class PersonaTestingApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.personaTestingApiUrl;

  getPersonas(): Observable<{ personas: PersonaInfo[] }> {
    return this.http.get<{ personas: PersonaInfo[] }>(`${this.baseUrl}/personas`);
  }

  getPersona(id: string): Observable<PersonaInfo> {
    return this.http.get<PersonaInfo>(
      `${this.baseUrl}/personas/${encodeURIComponent(id)}`,
    );
  }

  createPersona(payload: CreatePersonaRequest): Observable<PersonaInfo> {
    return this.http.post<PersonaInfo>(`${this.baseUrl}/personas`, payload);
  }

  updatePersona(id: string, payload: UpdatePersonaRequest): Observable<PersonaInfo> {
    return this.http.put<PersonaInfo>(
      `${this.baseUrl}/personas/${encodeURIComponent(id)}`,
      payload,
    );
  }

  deletePersona(id: string): Observable<void> {
    return this.http.delete<void>(
      `${this.baseUrl}/personas/${encodeURIComponent(id)}`,
    );
  }

  getTestableTeams(): Observable<{ teams: TestableTeam[] }> {
    return this.http.get<{ teams: TestableTeam[] }>(
      `${this.baseUrl}/testable-teams`,
    );
  }

  startTest(
    payload: StartTestRequest,
  ): Observable<{ job_id: string; status: string; message: string }> {
    return this.http.post<{ job_id: string; status: string; message: string }>(
      `${this.baseUrl}/start`,
      payload,
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

  resumeJob(jobId: string): Observable<unknown> {
    return this.http.post(`${this.baseUrl}/job/${encodeURIComponent(jobId)}/resume`, {});
  }

  restartJob(jobId: string): Observable<unknown> {
    return this.http.post(`${this.baseUrl}/job/${encodeURIComponent(jobId)}/restart`, {});
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
