import type { CodeReviewSummary, PendingIssueProposal } from './coding-team.model';

/** Integration list item (GET /api/integrations). */
export interface IntegrationListItem {
  id: string;
  type: string;
  enabled: boolean;
  channel: string | null;
}

export type SlackMode = 'webhook' | 'bot';

/** Slack config response (GET /api/integrations/slack). */
export interface SlackConfigResponse {
  enabled: boolean;
  mode: SlackMode;
  client_id_configured: boolean;
  /** True when a Slack app signing secret is stored (required to receive events/commands). */
  signing_secret_configured: boolean;
  webhook_url: string | null;
  webhook_configured: boolean;
  bot_token_configured: boolean;
  default_channel: string;
  channel_display_name: string;
  notify_open_questions: boolean;
  notify_pa_responses: boolean;
  /** True when the bot token was obtained via OAuth (workspace connected). */
  oauth_connected: boolean;
  /** Slack workspace name, populated after OAuth. */
  team_name: string | null;
  /** Slack workspace/team ID. */
  team_id: string | null;
}

/** Request body for PUT /api/integrations/slack. */
export interface SlackConfigUpdate {
  enabled: boolean;
  mode: SlackMode;
  client_id: string;
  client_secret: string;
  /** Slack app signing secret; omitted/empty preserves the existing one. */
  signing_secret: string;
  webhook_url: string;
  bot_token: string;
  default_channel: string;
  channel_display_name: string;
  notify_open_questions: boolean;
  notify_pa_responses: boolean;
}

/** Response for GET /api/integrations/slack/oauth/connect. */
export interface SlackOAuthConnectResponse {
  url: string;
  client_id: string;
}

/** Identity provider used on Medium.com (for UX; stats agent uses stored browser session). */
export type MediumOAuthProvider = 'google' | 'apple' | 'facebook' | 'twitter';

/** Medium config response (GET /api/integrations/medium). */
export interface MediumConfigResponse {
  enabled: boolean;
  oauth_provider: MediumOAuthProvider;
  oauth_identity_connected: boolean;
  google_client_configured: boolean;
  session_configured: boolean;
  linked_email: string | null;
  linked_name: string | null;
}

/** Request body for PUT /api/integrations/medium. */
export interface MediumConfigUpdate {
  enabled: boolean;
  oauth_provider: MediumOAuthProvider;
  google_client_id: string;
  google_client_secret: string;
}

/** POST /api/integrations/medium/session */
export interface MediumSessionImportBody {
  storage_state: Record<string, unknown>;
}

/** GET /api/integrations/google-browser-login */
export interface GoogleBrowserLoginStatusResponse {
  configured: boolean;
  /** False when the API has no Postgres (POSTGRES_HOST); credentials cannot be saved. */
  storage_available: boolean;
}

/** PUT /api/integrations/google-browser-login */
export interface GoogleBrowserLoginCredentialsBody {
  email: string;
  password: string;
}

/**
 * GitHub config response (GET /api/integrations/github). Repository access is
 * defined by the PAT's own authorization configuration — pages list every
 * accessible repo via GET /api/integrations/github/repos and pass an explicit
 * owner/repo per request, so no repository fields are modelled here (the backend
 * still returns a legacy optional default owner/repo, which the UI ignores).
 */
export interface GitHubConfigResponse {
  enabled: boolean;
  token_configured: boolean;
  default_label: string;
  /**
   * @deprecated Legacy configured default owner. Repository access now comes from
   * the PAT's own authorization; the UI never reads this. Typed as optional so the
   * compiler flags any accidental new usage.
   */
  owner?: string;
  /**
   * @deprecated Legacy configured default repo. Repository access now comes from
   * the PAT's own authorization; the UI never reads this. Typed as optional so the
   * compiler flags any accidental new usage.
   */
  repo?: string;
  /**
   * True when a webhook signing secret is configured (stored credential or
   * GITHUB_WEBHOOK_SECRET env var), used to verify "@khala review" PR-comment
   * webhooks delivered to POST /api/integrations/github/events.
   */
  webhook_secret_configured?: boolean;
  /**
   * True when Postgres (the PAT store) is configured but unreachable, so
   * `token_configured` may read false only because the store is down. Lets the
   * UI warn instead of showing a bare "not connected".
   */
  credential_store_unreachable?: boolean;
}

/**
 * Request body for PUT /api/integrations/github. No repository fields: which
 * repositories the integration can reach is decided by the PAT's own
 * authorization configuration, never configured in Khala.
 */
export interface GitHubConfigUpdate {
  enabled: boolean;
  token: string;
  default_label: string;
  repo_path: string;
  /** GitHub webhook signing secret; omitted/empty preserves the existing one. */
  webhook_secret?: string;
}

/**
 * One repository the configured PAT can access
 * (GET /api/integrations/github/repos). Mirrors GitHub's `GET /user/repos` for the
 * stored token, so the token's own authorization configuration is the source of truth.
 */
export interface GitHubRepoItem {
  owner: string;
  name: string;
  full_name: string;
  private: boolean;
  archived: boolean;
  html_url: string;
  description: string;
  default_branch: string;
  /** GitHub's count includes open PRs — an at-a-glance hint, not the exact issue total. */
  open_issues_count: number;
  pushed_at: string;
}

/** A single issue this issue is blocked by (a GitHub "blocked by" dependency). */
export interface GitHubDependencyRef {
  number: number;
  title: string;
  state: 'open' | 'closed';
}

/** Single GitHub issue item from GET /api/integrations/github/issues. */
export interface GitHubIssueItem {
  number: number;
  title: string;
  body_preview: string;
  labels: string[];
  html_url: string;
  /** All issues this issue is blocked by ("depends on"). */
  dependencies: GitHubDependencyRef[];
  /** Numbers of dependencies still open (drives the blocked indicator). */
  open_dependencies: number[];
  /** True while any dependency is still open. */
  blocked: boolean;
}

/** Request body for POST /api/integrations/github/run-issue. */
export interface RunGitHubIssueRequest {
  issue_number: number;
  base_branch?: string;
  /** Target repository; omitted falls back to the legacy configured default. */
  owner?: string;
  repo?: string;
}

/** Response from POST /api/integrations/github/run-issue. */
export interface RunGitHubIssueResponse {
  job_id: string;
  issue_number: number;
  issue_url: string;
  status: string;
  message: string;
}

/** Single open pull request from GET /api/integrations/github/pulls. */
export interface GitHubPullRequestItem {
  number: number;
  title: string;
  body_preview: string;
  author: string;
  html_url: string;
  head: string;
  base: string;
  draft: boolean;
  labels: string[];
  updated_at: string;
}

/** Request body for POST /api/integrations/github/review-pr. */
export interface RunPrReviewRequest {
  pr_number: number;
  base_branch?: string;
  /** Target repository; omitted falls back to the legacy configured default. */
  owner?: string;
  repo?: string;
}

/** Response from POST /api/integrations/github/review-pr. */
export interface RunPrReviewResponse {
  job_id: string;
  pr_number: number;
  pr_url: string;
  status: string;
  message: string;
  /** ISO-8601 server-clock start time of the review, used to compute a live
   * duration on server timestamps at both ends. Absent falls back to the browser clock. */
  created_at?: string;
}

/** Request body for POST /api/integrations/github/pulls/{pr_number}/address-comments. */
export interface AddressPrCommentsRequest {
  /** Target repository; omitted falls back to the legacy configured default. */
  owner?: string;
  repo?: string;
}

/** Response from POST /api/integrations/github/pulls/{pr_number}/address-comments. */
export interface AddressPrCommentsResponse {
  job_id: string;
  pr_number: number;
  pr_url: string;
  /** Number of unresolved review comments the started job will work through. */
  unresolved_comment_count: number;
  status: string;
  message: string;
  /** ISO-8601 server-clock start time of the job. */
  created_at?: string;
}

/** TradingView MCP config response (GET/PUT/DELETE /api/integrations/tradingview). */
export interface TradingViewConfigResponse {
  enabled: boolean;
  mcp_server_url: string;
  tool_name: string;
  /** True when an encrypted auth token is stored (the token itself is never returned). */
  auth_token_configured: boolean;
}

/** Request body for PUT /api/integrations/tradingview. */
export interface TradingViewConfigUpdate {
  enabled: boolean;
  mcp_server_url: string;
  tool_name: string;
  /** Bearer token / API key; empty preserves the existing stored token. */
  auth_token: string;
}

/** Result of POST /api/integrations/tradingview/test (a live reachability probe). */
export interface TradingViewTestResponse {
  /** True when the stored MCP server answered the OHLCV probe without error. */
  ok: boolean;
  /** Human-readable outcome — the reason for a failure, or a success note. */
  detail: string;
}

/**
 * One persisted code-review run for a pull request
 * (GET /api/integrations/github/reviews). Backed by the coding team's
 * `code_review_runs` table so review history survives reloads/restarts.
 */
export interface CodeReviewRunItem {
  job_id: string;
  pr_number: number;
  pr_url?: string;
  status: string;
  status_text?: string;
  review_summary?: CodeReviewSummary;
  error?: string;
  /** ISO-8601 timestamp when the review was started. */
  created_at: string;
  /** ISO-8601 timestamp when the review reached a terminal state, if any. */
  completed_at?: string;
}

/** Request body for POST /api/integrations/github/reviews/{job_id}/issues. */
export interface CreateReviewIssuesRequest {
  /** Ids of the review's pending issue proposals to file as GitHub issues. */
  proposal_ids: string[];
  /** Repository the review belongs to; validated server-side against the review. */
  owner: string;
  repo: string;
}

/** One GitHub issue opened from a review's pending issue proposal. */
export interface CreatedReviewIssueItem {
  proposal_id: string;
  issue_number: number;
  issue_url: string;
  title: string;
}

/** Response from POST /api/integrations/github/reviews/{job_id}/issues. */
export interface CreateReviewIssuesResponse {
  job_id: string;
  /** Issues opened by this request. */
  created: CreatedReviewIssueItem[];
  /** The review's full, updated pending-proposal list (filed ones now carry
   * `issue_url`/`issue_number`). */
  proposals: PendingIssueProposal[];
}

/** One LLM call the code-review pipeline made, in call order. */
export interface CodeReviewTranscriptEntry {
  stage: string;
  target: string;
  model: string;
  prompt: string;
  response: string;
  /** ISO-8601 timestamp the call started (backdated from its measured duration). */
  started_at: string;
  duration_ms: number;
}

/** Response from GET /api/integrations/github/reviews/{job_id}/transcript. */
export interface CodeReviewTranscript {
  job_id: string;
  entries: CodeReviewTranscriptEntry[];
}


// ---------------------------------------------------------------------------
// Out-of-scope issue proposals
// ---------------------------------------------------------------------------

/** One unfiled out-of-scope issue proposal surfaced by code reviews. */
export interface OutOfScopeProposalItem {
  id: string;
  job_id: string;
  pr_number: number;
  pr_url: string | null;
  severity: string;
  category: string;
  file_path: string;
  line: number | null;
  description: string;
  suggestion: string;
  locations: { file_path: string; line: number | null; description: string; suggestion: string }[];
  issue_number: number | null;
  issue_url: string | null;
}

/** Response from GET /api/integrations/github/reviews/out-of-scope-issues. */
export interface OutOfScopeProposalsResponse {
  owner: string;
  repo: string;
  proposals: OutOfScopeProposalItem[];
  total: number;
  unfiled: number;
}

/** Request body for POST /api/integrations/github/reviews/out-of-scope-issues/file. */
export interface FileOutOfScopeIssuesRequest {
  proposal_ids: string[];
  owner: string;
  repo: string;
}

/** One GitHub issue created via the enhanced issue builder. */
export interface EnhancedCreatedIssueItem {
  proposal_id: string;
  issue_number: number;
  issue_url: string;
  title: string;
  label: string;
  complexity_score: number;
  merged_into_existing: boolean;
}

/** Response from POST /api/integrations/github/reviews/out-of-scope-issues/file. */
export interface FileOutOfScopeIssuesResponse {
  created: EnhancedCreatedIssueItem[];
  errors: string[];
}
