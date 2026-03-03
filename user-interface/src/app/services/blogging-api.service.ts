import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  ResearchAndReviewRequest,
  ResearchAndReviewResponse,
  FullPipelineRequest,
  FullPipelineResponse,
  HealthResponse,
  BlogJobListItem,
  BlogJobStatusResponse,
} from '../models';

/**
 * Service for Blogging API endpoints.
 * Base URL from environment.bloggingApiUrl (default port 8000).
 */
@Injectable({ providedIn: 'root' })
export class BloggingApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.bloggingApiUrl;

  /**
   * POST /research-and-review
   * Runs research and review pipeline; returns title choices and outline.
   */
  researchAndReview(
    request: ResearchAndReviewRequest
  ): Observable<ResearchAndReviewResponse> {
    return this.http.post<ResearchAndReviewResponse>(
      `${this.baseUrl}/research-and-review`,
      request
    );
  }

  /**
   * POST /full-pipeline
   * Runs full blog pipeline with gates (research, review, draft, validators, compliance).
   */
  fullPipeline(request: FullPipelineRequest): Observable<FullPipelineResponse> {
    return this.http.post<FullPipelineResponse>(
      `${this.baseUrl}/full-pipeline`,
      request
    );
  }

  /**
   * GET /health
   * Health check for the Blogging API.
   */
  health(): Observable<HealthResponse> {
    return this.http.get<HealthResponse>(`${this.baseUrl}/health`);
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
}
