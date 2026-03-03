/** Request for POST /social-marketing/run. */
export interface RunMarketingTeamRequest {
  brand_guidelines_path: string;
  brand_objectives_path: string;
  llm_model_name: string;
  brand_name?: string;
  target_audience?: string;
  goals?: string[];
  voice_and_tone?: string;
  cadence_posts_per_day?: number;
  duration_days?: number;
  human_approved_for_testing?: boolean;
  human_feedback?: string;
}

/** Response from POST /social-marketing/run. */
export interface RunMarketingTeamResponse {
  job_id: string;
  status: string;
  message: string;
}

/** Post performance observation. */
export interface PostPerformanceObservation {
  platform?: string;
  post_id?: string;
  engagement_metrics?: Record<string, number>;
  [key: string]: unknown;
}

/** Request for POST /social-marketing/performance/{job_id}. */
export interface PerformanceIngestRequest {
  observations: PostPerformanceObservation[];
}

/** Response from POST /social-marketing/performance/{job_id}. */
export interface PerformanceIngestResponse {
  job_id: string;
  campaign_name?: string;
  observations_ingested: number;
  message: string;
}

/** Request for POST /social-marketing/revise/{job_id}. */
export interface ReviseMarketingTeamRequest {
  feedback: string;
  approved_for_testing?: boolean;
}

/** Item from GET /social-marketing/jobs (job list). */
export interface MarketingJobListItem {
  job_id: string;
  status: string;
  current_stage: string;
  progress: number;
  created_at?: string;
  last_updated_at?: string;
}

/** Response from GET /social-marketing/status/{job_id}. */
export interface MarketingJobStatusResponse {
  job_id: string;
  status: string;
  current_stage: string;
  progress: number;
  llm_model_name: string;
  brand_guidelines_path: string;
  brand_objectives_path: string;
  last_updated_at: string;
  eta_hint?: string;
  error?: string;
  result?: unknown;
}
