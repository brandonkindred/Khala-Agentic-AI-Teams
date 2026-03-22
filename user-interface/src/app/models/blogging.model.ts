/** GET /health from the Blogging API (extends generic status). */
export interface BloggingHealthResponse {
  status: string;
  /** True when brand_spec_prompt.md is present and substantive; UI hides audience/tone for full pipeline. */
  brand_spec_configured?: boolean;
}

/** Audience details for targeting content. */
export interface AudienceDetails {
  skill_level?: string;
  profession?: string;
  hobbies?: string[];
  other?: string;
}

/** Request for POST /research-and-review (research + planning; same length options as full pipeline). */
export interface ResearchAndReviewRequest {
  brief: string;
  title_concept?: string;
  audience?: AudienceDetails | string;
  tone_or_purpose?: string;
  max_results?: number;
  content_profile?: BlogContentProfile;
  series_context?: BlogSeriesContext;
  length_notes?: string;
  target_word_count?: number;
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

/**
 * Semantic writing format for POST /full-pipeline (matches backend `ContentProfile`).
 * Drives length guidance when `target_word_count` is omitted.
 */
export type BlogContentProfile =
  | 'short_listicle'
  | 'standard_article'
  | 'technical_deep_dive'
  | 'series_instalment';

/** Optional scope when the post is one instalment of a series (matches backend `SeriesContext`). */
export interface BlogSeriesContext {
  series_title?: string;
  part_number?: number;
  planned_parts?: number;
  instalment_scope?: string;
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
  /** Overrides numeric target when set; otherwise length comes from `content_profile`. */
  target_word_count?: number;
  content_profile?: BlogContentProfile;
  series_context?: BlogSeriesContext;
  length_notes?: string;
}

/** Response from POST /full-pipeline. */
export interface FullPipelineResponse {
  status: string;
  work_dir: string;
  title_choices: TitleChoiceResponse[];
  outline: string;
  draft_preview?: string;
  /** Short summary from the approved ContentPlan (topic + narrative flow). */
  content_plan_summary?: string;
}

/** Request for POST /medium-stats and /medium-stats-async. */
export interface MediumStatsRequest {
  headless?: boolean;
  timeout_ms?: number;
  max_posts?: number;
}

/** Summary item for GET /jobs (blog pipeline job list). */
export interface BlogJobListItem {
  job_id: string;
  status: string;
  brief: string;
  phase?: string;
  progress: number;
  created_at?: string;
  /** Set to `medium_stats` for Medium statistics jobs. */
  job_type?: string | null;
}

/** A story gap opportunity identified by the ghost writer agent. */
export interface BlogStoryGap {
  section_title: string;
  section_context: string;
  seed_question: string;
}

/** A message in the story elicitation chat. */
export interface BlogStoryChatMessage {
  role: 'agent' | 'user';
  content: string;
  gap_index?: number;
}

/** A pending question from a pipeline agent waiting for author input. */
export interface BlogPendingQuestion {
  id: string;
  question_text: string;
  context?: string;
  options?: Array<{ id: string; label: string; description?: string }>;
  required?: boolean;
  allow_multiple?: boolean;
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
  approved_at?: string;
  approved_by?: string;
  job_type?: string | null;
  content_plan_summary?: string;
  planning_iterations_used?: number;
  parse_retry_count?: number;
  planning_wall_ms_total?: number;
  /** When failed during planning, machine-readable reason (e.g. parse_failure). */
  planning_failure_reason?: string;
  // Title selection
  waiting_for_title_selection?: boolean;
  selected_title?: string;
  // Story elicitation
  waiting_for_story_input?: boolean;
  story_gaps?: BlogStoryGap[];
  current_story_gap_index?: number;
  story_chat_history?: BlogStoryChatMessage[];
  elicited_stories?: string[];
  // General Q&A
  pending_questions?: BlogPendingQuestion[];
  waiting_for_answers?: boolean;
}

/** Metadata for a single artifact (from GET /job/{id}/artifacts). */
export interface ArtifactMeta {
  name: string;
  producer_phase?: string;
  producer_agent?: string;
}

/** Response from POST /full-pipeline-async or POST /research-and-review-async. */
export interface StartJobResponse {
  job_id: string;
  message?: string;
}

/** Response from GET /job/{job_id}/artifacts (list of artifacts with metadata). */
export interface BlogJobArtifactsResponse {
  artifacts: ArtifactMeta[];
}

/** Response from GET /job/{job_id}/artifacts/{name} (single artifact content). */
export interface BlogJobArtifactContentResponse {
  name: string;
  content: string | object;
}
