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

export type IntegrationFieldType = 'text' | 'password' | 'url' | 'textarea';

export interface IntegrationFieldDefinition {
  key: string;
  label: string;
  type: IntegrationFieldType;
  placeholder?: string;
  required?: boolean;
  helpText?: string;
}

export interface IntegrationDefinition {
  id: string;
  name: string;
  category: string;
  description: string;
  icon: string;
  fields: IntegrationFieldDefinition[];
}

export const INTEGRATIONS: IntegrationDefinition[] = [
  {
    id: 'slack',
    name: 'Slack',
    category: 'Communication',
    description: 'Send alerts, open questions, and workflow updates to Slack channels.',
    icon: 'forum',
    fields: [
      { key: 'workspace', label: 'Workspace', type: 'text', placeholder: 'your-company', required: true },
      { key: 'channel', label: 'Channel', type: 'text', placeholder: 'engineering-alerts', required: true },
      { key: 'webhookUrl', label: 'Incoming Webhook URL', type: 'url', required: true },
    ],
  },
  {
    id: 'github',
    name: 'GitHub',
    category: 'Source Control',
    description: 'Connect repositories for PR automation and development workflows.',
    icon: 'code',
    fields: [
      { key: 'organization', label: 'Organization', type: 'text', required: true },
      { key: 'repository', label: 'Default Repository', type: 'text', required: true },
      { key: 'token', label: 'Personal Access Token', type: 'password', required: true },
    ],
  },
  {
    id: 'jira',
    name: 'Jira',
    category: 'Project Management',
    description: 'Sync planning artifacts and implementation status with Jira issues.',
    icon: 'assignment',
    fields: [
      { key: 'siteUrl', label: 'Jira Site URL', type: 'url', required: true },
      { key: 'projectKey', label: 'Project Key', type: 'text', required: true },
      { key: 'email', label: 'Service Account Email', type: 'text', required: true },
      { key: 'apiToken', label: 'API Token', type: 'password', required: true },
    ],
  },
  {
    id: 'figma',
    name: 'Figma',
    category: 'Design',
    description: 'Import design tokens and review component specs from Figma.',
    icon: 'palette',
    fields: [
      { key: 'teamId', label: 'Team ID', type: 'text', required: true },
      { key: 'projectId', label: 'Project ID', type: 'text', required: true },
      { key: 'accessToken', label: 'Access Token', type: 'password', required: true },
    ],
  },
  {
    id: 'notion',
    name: 'Notion',
    category: 'Documentation',
    description: 'Pull product requirements and write summaries to Notion workspaces.',
    icon: 'description',
    fields: [
      { key: 'workspace', label: 'Workspace Name', type: 'text', required: true },
      { key: 'databaseId', label: 'Database ID', type: 'text', required: true },
      { key: 'integrationToken', label: 'Integration Token', type: 'password', required: true },
    ],
  },
];

export function getIntegrationById(integrationId: string): IntegrationDefinition | undefined {
  return INTEGRATIONS.find((integration) => integration.id === integrationId);
}
