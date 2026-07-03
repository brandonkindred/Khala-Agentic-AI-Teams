/**
 * Job Matching team models — snake_case mirrors of the backend Pydantic models
 * in backend/agents/job_matching_team/models.py and profile/model.py.
 */

/** remote | hybrid | onsite | any */
export type RemotePreference = 'remote' | 'hybrid' | 'onsite' | 'any';

/** Ranker recommendation for a posting. */
export type Recommendation = 'apply' | 'maybe' | 'skip';

/** Relative importance of each scoring dimension (need not sum to 1). */
export interface RankingWeights {
  title_fit: number;
  seniority_fit: number;
  location_fit: number;
  comp_fit: number;
  company_fit: number;
  skills_fit: number;
}

/**
 * The user's standing job-search criteria — persisted as the "career" section
 * of the central user profile (PUT /profile).
 */
export interface JobSeekerProfile {
  target_titles: string[];
  seniority_levels: string[];
  locations: string[];
  remote_preference: RemotePreference;
  salary_min: number;
  currency: string;
  company_stages: string[];
  company_sizes: string[];
  industries: string[];
  must_have_skills: string[];
  nice_to_have_skills: string[];
  deal_breakers: string[];
  preferred_companies: string[];
  excluded_companies: string[];
  work_authorization: string;
  keywords: string[];
  weights: RankingWeights;
}

/** A single open role discovered and normalized from the web. */
export interface JobPosting {
  title: string;
  company: string;
  location: string;
  remote_mode: 'remote' | 'hybrid' | 'onsite' | 'unknown';
  salary_min?: number | null;
  salary_max?: number | null;
  currency: string;
  url: string;
  source: string;
  description: string;
  posted_at?: string | null;
  fingerprint: string;
}

/** Per-dimension fit scores, each in [0, 1]. */
export interface SubScores {
  title_fit: number;
  seniority_fit: number;
  location_fit: number;
  comp_fit: number;
  company_fit: number;
  skills_fit: number;
}

/** A posting plus its computed score, recommendation, and rationale. */
export interface RankedJob {
  posting: JobPosting;
  score: number;
  sub_scores: SubScores;
  recommendation: Recommendation;
  rationale: string;
  concerns: string[];
}

/** Parameters for a single scan-and-rank run (POST /scan). */
export interface JobMatchRequest {
  profile_overrides?: Partial<JobSeekerProfile> | null;
  max_queries?: number;
  max_roles?: number;
  top_n?: number;
  exclude_seen?: boolean;
}

/** The ranked result of a completed scan run. */
export interface JobMatchResponse {
  run_id: string;
  ranked_jobs: RankedJob[];
  total_found: number;
  total_ranked: number;
  profile_snapshot: JobSeekerProfile;
  generated_at: string;
}

/** Async scan-job status (GET /scan/status/{job_id}). */
export interface JobMatchScanJob {
  job_id: string;
  status: string;
  result?: JobMatchResponse | null;
  error?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

/** Row in the scan-jobs list (GET /scan/jobs). */
export interface ScanJobListItem {
  job_id: string;
  status: string;
  created_at?: string | null;
  updated_at?: string | null;
}

/** Persisted run summary (GET /runs). */
export interface JobMatchRunSummary {
  run_id: string;
  status: string;
  total_found: number;
  total_ranked: number;
  created_at?: string | null;
  completed_at?: string | null;
}

/** A run plus its ranked jobs (GET /runs/{run_id}). */
export interface JobMatchRunDetail extends JobMatchRunSummary {
  error?: string | null;
  ranked_jobs: RankedJob[];
}

/** Exclusive user disposition of a listing; 'new' is the untriaged default. */
export type ListingStatus = 'new' | 'favorite' | 'not_interested' | 'poor_fit' | 'archived';

/**
 * GET /listings filter: every ListingStatus plus 'active' (everything except
 * archived and not_interested) and 'all'.
 */
export type ListingFilter = 'active' | 'all' | ListingStatus;

/** Latest ranked snapshot of a posting (per fingerprint) plus its user state. */
export interface Listing {
  fingerprint: string;
  posting: JobPosting;
  score: number;
  sub_scores: SubScores;
  recommendation: Recommendation;
  rationale: string;
  concerns: string[];
  run_id: string;
  last_seen_at?: string | null;
  times_seen: number;
  status: ListingStatus;
  notes?: string | null;
  status_updated_at?: string | null;
}

/** Aggregated listings plus per-status counts (drives the filter pills). */
export interface ListingsResponse {
  listings: Listing[];
  total: number;
  counts: Record<string, number>;
}

/** PATCH /listings/{fingerprint} body. */
export interface ListingStateUpdate {
  status: ListingStatus;
  notes?: string | null;
}
