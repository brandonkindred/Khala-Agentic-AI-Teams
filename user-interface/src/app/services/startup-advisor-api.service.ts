import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, EMPTY, of, throwError, timer } from 'rxjs';
import { expand, first, switchMap } from 'rxjs/operators';
import { environment } from '../../environments/environment';
import type {
  StartupAdvisorConversationState,
  StartupAdvisorArtifact,
  StartupAdvisorUpdateContextRequest,
} from '../models';

interface AdvisorJobSubmission {
  job_id: string;
  status: string;
}

interface AdvisorJobStatus {
  job_id: string;
  status: string;
  result?: StartupAdvisorConversationState | null;
  error?: string | null;
}

const ADVISOR_POLL_INTERVAL_MS = 1500;

@Injectable({ providedIn: 'root' })
export class StartupAdvisorApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.startupAdvisorApiUrl;

  /** GET /conversation — get or create the singleton conversation */
  getConversation(): Observable<StartupAdvisorConversationState> {
    return this.http.get<StartupAdvisorConversationState>(`${this.baseUrl}/conversation`);
  }

  /** POST /conversation/messages — submit a message; poll until the advisor reply lands. */
  sendMessage(message: string): Observable<StartupAdvisorConversationState> {
    return this.http
      .post<AdvisorJobSubmission>(`${this.baseUrl}/conversation/messages`, { message })
      .pipe(switchMap((submission) => this.pollMessageJob(submission.job_id)));
  }

  private pollMessageJob(jobId: string): Observable<StartupAdvisorConversationState> {
    const statusUrl = `${this.baseUrl}/conversation/messages/status/${encodeURIComponent(jobId)}`;
    const poll$ = this.http.get<AdvisorJobStatus>(statusUrl);
    return poll$.pipe(
      expand((job) =>
        job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled'
          ? EMPTY
          : timer(ADVISOR_POLL_INTERVAL_MS).pipe(switchMap(() => poll$))
      ),
      first((job) =>
        job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled'
      ),
      switchMap((job) =>
        job.status === 'completed' && job.result
          ? of(job.result)
          : throwError(() => new Error(job.error || `Startup advisor job ${job.status}`))
      )
    );
  }

  /** GET /conversation/artifacts — list all artifacts */
  getArtifacts(): Observable<StartupAdvisorArtifact[]> {
    return this.http.get<StartupAdvisorArtifact[]>(`${this.baseUrl}/conversation/artifacts`);
  }

  /** PUT /conversation/context — manually update the founder profile context */
  updateContext(context: Record<string, string>): Observable<StartupAdvisorConversationState> {
    return this.http.put<StartupAdvisorConversationState>(
      `${this.baseUrl}/conversation/context`,
      { context } as StartupAdvisorUpdateContextRequest
    );
  }
}
