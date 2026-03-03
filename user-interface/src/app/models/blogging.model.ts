/** Audience details for targeting content. */
export interface AudienceDetails {
  skill_level?: string;
  profession?: string;
  hobbies?: string[];
  other?: string;
}

/** Request for POST /research-and-review. */
export interface ResearchAndReviewRequest {
  brief: string;
  title_concept?: string;
  audience?: AudienceDetails | string;
  tone_or_purpose?: string;
  max_results?: number;
}

/** A title choice with probability of success. */
export interface TitleChoiceResponse {
  title: string;
  probability_of_success: number;
}

/** Response from POST /research-and-review. */
export interface ResearchAndReviewResponse {
  title_choices: TitleChoiceResponse[];
  outline: string;
  compiled_document?: string;
  notes?: string;
}

/** Request for POST /full-pipeline. */
export interface FullPipelineRequest {
  brief: string;
  title_concept?: string;
  audience?: AudienceDetails | string;
  tone_or_purpose?: string;
  max_results?: number;
  run_gates?: boolean;
  max_rewrite_iterations?: number;
}

/** Response from POST /full-pipeline. */
export interface FullPipelineResponse {
  status: string;
  work_dir: string;
  title_choices: TitleChoiceResponse[];
  outline: string;
  draft_preview?: string;
}

/** Summary item for GET /jobs (blog pipeline job list). */
export interface BlogJobListItem {
  job_id: string;
  status: string;
  brief: string;
  phase?: string;
  progress: number;
  created_at?: string;
}

/** Full status for GET /job/{job_id} (blog pipeline job polling). */
export interface BlogJobStatusResponse {
  job_id: string;
  status: string;
  phase?: string;
  progress: number;
  status_text?: string;
  error?: string;
  failed_phase?: string;
  title_choices: TitleChoiceResponse[];
  outline?: string;
  draft_preview?: string;
  work_dir?: string;
  research_sources_count: number;
  draft_iterations: number;
  rewrite_iterations: number;
  created_at?: string;
  started_at?: string;
  completed_at?: string;
}

/** Response from POST /full-pipeline-async or POST /research-and-review-async. */
export interface StartJobResponse {
  job_id: string;
  message?: string;
}
