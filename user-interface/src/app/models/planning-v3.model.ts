/** Planning V3 Team API models (client-facing discovery / PRD). */

import type { PendingQuestion } from './software-engineering.model';

/** Request for POST /run */
export interface PlanningV3RunRequest {
  repo_path: string;
  client_name?: string;
  initial_brief?: string;
  spec_content?: string;
  use_product_analysis?: boolean;
  use_planning_v2?: boolean;
  use_market_research?: boolean;
}

/** Response from POST /run */
export interface PlanningV3RunResponse {
  job_id: string;
  status: string;
  message: string;
}

/** Response from GET /status/{job_id} */
export interface PlanningV3StatusResponse {
  job_id: string;
  status: string;
  repo_path?: string;
  current_phase?: string;
  status_text?: string;
  progress: number;
  pending_questions: PendingQuestion[];
  waiting_for_answers: boolean;
  error?: string;
  summary?: string;
}

/** Response from GET /result/{job_id} */
export interface PlanningV3ResultResponse {
  job_id: string;
  success: boolean;
  handoff_package?: Record<string, unknown>;
  client_context_document_path?: string;
  validated_spec_path?: string;
  prd_path?: string;
  summary?: string;
  failure_reason?: string;
}

/** Job summary for GET /jobs */
export interface PlanningV3JobSummary {
  job_id: string;
  status: string;
  repo_path?: string;
  current_phase?: string;
}

/** Response from GET /jobs */
export interface PlanningV3JobsResponse {
  jobs: PlanningV3JobSummary[];
}
