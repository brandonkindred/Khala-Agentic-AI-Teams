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

/** GitHub config response (GET /api/integrations/github). */
export interface GitHubConfigResponse {
  enabled: boolean;
  token_configured: boolean;
  owner: string;
  repo: string;
  default_label: string;
}

/** Request body for PUT /api/integrations/github. */
export interface GitHubConfigUpdate {
  enabled: boolean;
  owner: string;
  repo: string;
  token: string;
  default_label: string;
  repo_path: string;
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
}

/** Response from POST /api/integrations/github/run-issue. */
export interface RunGitHubIssueResponse {
  job_id: string;
  issue_number: number;
  issue_url: string;
  status: string;
  message: string;
}
