import { Injectable, inject, NgZone } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  FullPipelineRequest,
  FullPipelineResponse,
  BloggingHealthResponse,
  BlogJobListItem,
  BlogJobStatusResponse,
  BlogJobStreamEvent,
  BlogJobArtifactsResponse,
  BlogJobArtifactContentResponse,
  StartJobResponse,
  MediumStatsRequest,
} from '../models';

/**
 * Service for Blogging API endpoints.
 * Base URL from environment.bloggingApiUrl (default port 8000).
 */
@Injectable({ providedIn: 'root' })
export class BloggingApiService {
  private readonly http = inject(HttpClient);
  private readonly zone = inject(NgZone);
  private readonly baseUrl = environment.bloggingApiUrl;

  /**
   * POST /full-pipeline
   * Runs full blog pipeline with gates (research, planning, draft, validators, compliance).
   */
  fullPipeline(request: FullPipelineRequest): Observable<FullPipelineResponse> {
    return this.http.post<FullPipelineResponse>(
      `${this.baseUrl}/full-pipeline`,
      request
    );
  }

  /**
   * POST /full-pipeline-async
   * Starts the full pipeline in the background. Returns job_id for polling.
   */
  startFullPipelineAsync(request: FullPipelineRequest): Observable<StartJobResponse> {
    return this.http.post<StartJobResponse>(
      `${this.baseUrl}/full-pipeline-async`,
      request
    );
  }

  /**
   * POST /medium-stats-async — background Medium statistics job.
   */
  startMediumStatsAsync(request: MediumStatsRequest): Observable<StartJobResponse> {
    return this.http.post<StartJobResponse>(`${this.baseUrl}/medium-stats-async`, request);
  }

  /**
   * POST /medium-stats — synchronous scrape (can take minutes).
   */
  mediumStatsSync(request: MediumStatsRequest): Observable<unknown> {
    return this.http.post(`${this.baseUrl}/medium-stats`, request);
  }

  /**
   * GET /health
   * Health check for the Blogging API (includes brand_spec_configured when supported).
   */
  health(): Observable<BloggingHealthResponse> {
    return this.http.get<BloggingHealthResponse>(`${this.baseUrl}/health`);
  }

  /**
   * GET /jobs
   * List blog pipeline jobs, optionally only running/pending.
   */
  getJobs(running_only?: boolean): Observable<BlogJobListItem[]> {
    let params = new HttpParams();
    if (running_only !== undefined) {
      params = params.set('running_only', String(running_only));
    }
    return this.http.get<BlogJobListItem[]>(`${this.baseUrl}/jobs`, { params });
  }

  /**
   * GET /job/{job_id}
   * Get status of a single blog pipeline job.
   */
  getJobStatus(job_id: string): Observable<BlogJobStatusResponse> {
    return this.http.get<BlogJobStatusResponse>(`${this.baseUrl}/job/${job_id}`);
  }

  /**
   * POST /job/{job_id}/cancel
   * Cancel a pending or running blog pipeline job.
   */
  cancelJob(jobId: string): Observable<{ job_id: string; status: string; message: string }> {
    return this.http.post<{ job_id: string; status: string; message: string }>(
      `${this.baseUrl}/job/${jobId}/cancel`,
      {}
    );
  }

  /**
   * DELETE /job/{job_id}
   * Delete a blog pipeline job from the store.
   */
  deleteJob(jobId: string): Observable<{ job_id: string; message: string }> {
    return this.http.delete<{ job_id: string; message: string }>(
      `${this.baseUrl}/job/${jobId}`
    );
  }

  /**
   * POST /job/{job_id}/approve
   * Mark a completed or needs_human_review job as approved.
   */
  approveJob(jobId: string): Observable<BlogJobStatusResponse> {
    return this.http.post<BlogJobStatusResponse>(
      `${this.baseUrl}/job/${jobId}/approve`,
      {}
    );
  }

  /**
   * POST /job/{job_id}/unapprove
   * Clear approval for a job.
   */
  unapproveJob(jobId: string): Observable<BlogJobStatusResponse> {
    return this.http.post<BlogJobStatusResponse>(
      `${this.baseUrl}/job/${jobId}/unapprove`,
      {}
    );
  }

  /**
   * GET /job/{job_id}/artifacts
   * List artifacts that exist for a pipeline job (with producer metadata).
   */
  getJobArtifacts(jobId: string): Observable<BlogJobArtifactsResponse> {
    return this.http.get<BlogJobArtifactsResponse>(`${this.baseUrl}/job/${jobId}/artifacts`);
  }

  /**
   * GET /job/{job_id}/artifacts/{artifact_name}
   * Get content of a single artifact for a job.
   */
  getJobArtifactContent(jobId: string, artifactName: string): Observable<BlogJobArtifactContentResponse> {
    return this.http.get<BlogJobArtifactContentResponse>(
      `${this.baseUrl}/job/${jobId}/artifacts/${encodeURIComponent(artifactName)}`
    );
  }

  /**
   * URL for downloading an artifact (GET with ?download=true).
   * Open in new window or use as href with download attribute to trigger save.
   */
  getJobArtifactDownloadUrl(jobId: string, artifactName: string): string {
    return `${this.baseUrl}/job/${jobId}/artifacts/${encodeURIComponent(artifactName)}?download=true`;
  }

  /**
   * POST /job/{job_id}/select-title
   * Submit the author-chosen title to resume the pipeline.
   */
  selectTitle(jobId: string, title: string): Observable<BlogJobStatusResponse> {
    return this.http.post<BlogJobStatusResponse>(
      `${this.baseUrl}/job/${jobId}/select-title`,
      { title }
    );
  }

  /**
   * POST /job/{job_id}/rate-titles
   * Rate titles as dislike/like/love. Love selects the title. Otherwise new titles are generated.
   */
  rateTitles(jobId: string, ratings: { title: string; rating: 'dislike' | 'like' | 'love' }[]): Observable<BlogJobStatusResponse> {
    return this.http.post<BlogJobStatusResponse>(
      `${this.baseUrl}/job/${jobId}/rate-titles`,
      { ratings }
    );
  }

  /**
   * POST /job/{job_id}/story-response
   * Send a message in the story elicitation conversation.
   */
  submitStoryResponse(jobId: string, message: string): Observable<BlogJobStatusResponse> {
    return this.http.post<BlogJobStatusResponse>(
      `${this.baseUrl}/job/${jobId}/story-response`,
      { message }
    );
  }

  /**
   * POST /job/{job_id}/skip-story-gap
   * Skip the current story gap.
   */
  skipStoryGap(jobId: string): Observable<BlogJobStatusResponse> {
    return this.http.post<BlogJobStatusResponse>(
      `${this.baseUrl}/job/${jobId}/skip-story-gap`,
      {}
    );
  }

  /**
   * POST /job/{job_id}/draft-feedback
   * Submit editor feedback on a draft or approve it.
   */
  submitDraftFeedback(jobId: string, feedback: string, approved: boolean): Observable<BlogJobStatusResponse> {
    return this.http.post<BlogJobStatusResponse>(
      `${this.baseUrl}/job/${jobId}/draft-feedback`,
      { feedback, approved }
    );
  }

  /**
   * POST /job/{job_id}/answers
   * Submit answers to pipeline Q&A questions.
   */
  submitBlogAnswers(jobId: string, answers: object[]): Observable<BlogJobStatusResponse> {
    return this.http.post<BlogJobStatusResponse>(
      `${this.baseUrl}/job/${jobId}/answers`,
      { answers }
    );
  }

  /**
   * GET /job/{job_id}/stream — SSE stream for real-time job updates.
   * Emits BlogJobStreamEvent objects. Completes on terminal event or connection close.
   */
  streamJobStatus(jobId: string): Observable<BlogJobStreamEvent> {
    return new Observable<BlogJobStreamEvent>((subscriber) => {
      const url = `${this.baseUrl}/job/${encodeURIComponent(jobId)}/stream`;
      const eventSource = new EventSource(url);

      eventSource.onmessage = (msg) => {
        this.zone.run(() => {
          try {
            const data: BlogJobStreamEvent = JSON.parse(msg.data);
            subscriber.next(data);
            if (data.type === 'done') {
              eventSource.close();
              subscriber.complete();
            }
          } catch {
            // Ignore unparseable frames (e.g. keepalive comments)
          }
        });
      };

      eventSource.onerror = () => {
        eventSource.close();
        this.zone.run(() => subscriber.error(new Error('SSE connection lost')));
      };

      return () => eventSource.close();
    });
  }
}
