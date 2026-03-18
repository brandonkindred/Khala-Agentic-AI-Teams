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
