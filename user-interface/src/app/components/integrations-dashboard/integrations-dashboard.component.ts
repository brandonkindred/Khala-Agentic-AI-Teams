import { Component, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute } from '@angular/router';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { MatIconModule } from '@angular/material/icon';
import { MatRadioModule } from '@angular/material/radio';
import { MatDividerModule } from '@angular/material/divider';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import type { SlackConfigResponse, SlackConfigUpdate, SlackMode } from '../../models/integrations.model';

const SLACK_WEBHOOK_PREFIX = 'https://hooks.slack.com/';

@Component({
  selector: 'app-integrations-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatSlideToggleModule,
    MatIconModule,
    MatRadioModule,
    MatDividerModule,
    MatProgressSpinnerModule,
  ],
  templateUrl: './integrations-dashboard.component.html',
  styleUrl: './integrations-dashboard.component.scss',
})
export class IntegrationsDashboardComponent implements OnInit {
  private readonly api = inject(IntegrationsApiService);
  private readonly route = inject(ActivatedRoute);

  loading = false;
  saving = false;
  connecting = false;
  disconnecting = false;
  error: string | null = null;
  success: string | null = null;

  // OAuth connection state
  oauthConnected = false;
  teamName: string | null = null;
  teamId: string | null = null;

  // App credentials for OAuth
  clientId = '';
  clientSecret = '';
  clientIdConfigured = false;

  // Shared settings (shown after any connection)
  slackEnabled = false;
  defaultChannel = '';
  channelDisplayName = '';
  notifyOpenQuestions = true;
  notifyPaResponses = true;

  // Advanced / manual mode
  showAdvanced = false;
  mode: SlackMode = 'webhook';
  webhookUrl = '';
  botToken = '';
  webhookConfigured = false;
  botTokenConfigured = false;

  ngOnInit(): void {
    this.loadSlackConfig();
    this.handleOAuthCallback();
  }

  /** Read ?slack_connected and ?slack_error query params set by the backend after OAuth. */
  private handleOAuthCallback(): void {
    this.route.queryParams.subscribe((params) => {
      if (params['slack_connected']) {
        const team = params['team'] ? decodeURIComponent(params['team']) : null;
        this.success = team
          ? `Connected to "${team}" workspace successfully.`
          : 'Slack connected successfully.';
        this.loadSlackConfig();
      } else if (params['slack_error']) {
        const errCode = params['slack_error'];
        this.error = this.friendlyOAuthError(errCode);
      }
    });
  }

  private friendlyOAuthError(code: string): string {
    const map: Record<string, string> = {
      access_denied: 'You cancelled the Slack authorization.',
      missing_code_or_state: 'Invalid OAuth response from Slack.',
      invalid_state: 'OAuth session expired or was tampered with. Please try again.',
      token_exchange_failed: 'Failed to exchange the authorization code. Check server logs.',
      missing_credentials: 'App credentials were not found. Please re-enter your Client ID and Secret.',
    };
    return map[code] ?? `Slack OAuth error: ${code}`;
  }

  loadSlackConfig(): void {
    this.loading = true;
    this.api.getSlackConfig().subscribe({
      next: (res: SlackConfigResponse) => {
        this.applyConfig(res);
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail || err?.message || 'Failed to load Slack config';
        this.loading = false;
      },
    });
  }

  private applyConfig(res: SlackConfigResponse): void {
    this.oauthConnected = res.oauth_connected ?? false;
    this.teamName = res.team_name ?? null;
    this.teamId = res.team_id ?? null;
    this.slackEnabled = res.enabled;
    this.mode = res.mode || 'webhook';
    this.clientIdConfigured = res.client_id_configured ?? false;
    this.webhookConfigured = res.webhook_configured;
    this.botTokenConfigured = res.bot_token_configured;
    this.defaultChannel = res.default_channel || '';
    this.channelDisplayName = res.channel_display_name || '';
    this.notifyOpenQuestions = res.notify_open_questions ?? true;
    this.notifyPaResponses = res.notify_pa_responses ?? true;
    // Never repopulate secrets from response
    this.webhookUrl = '';
    this.botToken = '';
    this.clientId = '';
    this.clientSecret = '';
  }

  // ---------------------------------------------------------------------------
  // OAuth flow
  // ---------------------------------------------------------------------------

  connectWithSlack(): void {
    this.connecting = true;
    this.error = null;
    this.success = null;

    const clientId = this.clientId.trim();
    const clientSecret = this.clientSecret.trim();

    const doConnect = () => {
      this.api.getSlackOAuthUrl().subscribe({
        next: (res) => {
          window.location.href = res.url;
        },
        error: (err) => {
          this.error = err?.error?.detail || err?.message || 'Failed to start Slack OAuth.';
          this.connecting = false;
        },
      });
    };

    // If credentials were entered, save them first before initiating OAuth
    if (clientId || clientSecret) {
      const body: SlackConfigUpdate = {
        enabled: this.slackEnabled,
        mode: this.mode,
        client_id: clientId,
        client_secret: clientSecret,
        webhook_url: '',
        bot_token: '',
        default_channel: this.defaultChannel.trim(),
        channel_display_name: this.channelDisplayName.trim(),
        notify_open_questions: this.notifyOpenQuestions,
        notify_pa_responses: this.notifyPaResponses,
      };
      this.api.updateSlackConfig(body).subscribe({
        next: (res) => {
          this.applyConfig(res);
          doConnect();
        },
        error: (err) => {
          this.error = err?.error?.detail || err?.message || 'Failed to save credentials.';
          this.connecting = false;
        },
      });
    } else {
      doConnect();
    }
  }

  disconnectSlack(): void {
    this.disconnecting = true;
    this.error = null;
    this.success = null;
    this.api.disconnectSlack().subscribe({
      next: (res) => {
        this.applyConfig(res);
        this.success = 'Slack disconnected.';
        this.disconnecting = false;
      },
      error: (err) => {
        this.error = err?.error?.detail || err?.message || 'Failed to disconnect Slack.';
        this.disconnecting = false;
      },
    });
  }

  // ---------------------------------------------------------------------------
  // Settings save (channel, toggles, enable/disable — after OAuth connection)
  // ---------------------------------------------------------------------------

  saveSettings(): void {
    const defaultChannel = this.defaultChannel.trim();

    this.saving = true;
    this.error = null;
    this.success = null;

    const body: SlackConfigUpdate = {
      enabled: this.slackEnabled,
      mode: this.mode,
      client_id: '',
      client_secret: '',
      webhook_url: '',
      bot_token: '',
      default_channel: defaultChannel,
      channel_display_name: this.channelDisplayName.trim(),
      notify_open_questions: this.notifyOpenQuestions,
      notify_pa_responses: this.notifyPaResponses,
    };

    this.api.updateSlackConfig(body).subscribe({
      next: (res) => {
        this.applyConfig(res);
        this.success = 'Settings saved.';
        this.saving = false;
      },
      error: (err) => {
        this.error = err?.error?.detail || err?.message || 'Failed to save settings.';
        this.saving = false;
      },
    });
  }

  // ---------------------------------------------------------------------------
  // Advanced (manual) mode save
  // ---------------------------------------------------------------------------

  webhookUrlInvalid(): boolean {
    const u = (this.webhookUrl || '').trim();
    if (!u) return false;
    return !u.startsWith(SLACK_WEBHOOK_PREFIX) || u.length < 50;
  }

  botTokenInvalid(): boolean {
    const token = (this.botToken || '').trim();
    if (!token) return false;
    return !token.startsWith('xoxb-');
  }

  saveAdvanced(): void {
    const webhookUrl = this.webhookUrl.trim();
    const botToken = this.botToken.trim();
    const defaultChannel = this.defaultChannel.trim();

    if (this.slackEnabled && this.mode === 'webhook') {
      if (!webhookUrl && !this.webhookConfigured) {
        this.error = 'Webhook URL is required for webhook mode.';
        return;
      }
      if (webhookUrl && this.webhookUrlInvalid()) {
        this.error = 'Webhook URL must start with https://hooks.slack.com/ and be complete.';
        return;
      }
    }

    if (this.slackEnabled && this.mode === 'bot') {
      if (!botToken && !this.botTokenConfigured) {
        this.error = 'Bot token is required for bot mode.';
        return;
      }
      if (botToken && this.botTokenInvalid()) {
        this.error = 'Bot token must start with xoxb-';
        return;
      }
      if (!defaultChannel) {
        this.error = 'Default channel is required for bot mode.';
        return;
      }
    }

    this.saving = true;
    this.error = null;
    this.success = null;

    const body: SlackConfigUpdate = {
      enabled: this.slackEnabled,
      mode: this.mode,
      client_id: this.clientId.trim(),
      client_secret: this.clientSecret.trim(),
      webhook_url: webhookUrl,
      bot_token: botToken,
      default_channel: defaultChannel,
      channel_display_name: this.channelDisplayName.trim(),
      notify_open_questions: this.notifyOpenQuestions,
      notify_pa_responses: this.notifyPaResponses,
    };

    this.api.updateSlackConfig(body).subscribe({
      next: (res) => {
        this.applyConfig(res);
        this.success = 'Slack integration saved.';
        this.saving = false;
      },
      error: (err) => {
        this.error = err?.error?.detail || err?.message || 'Failed to save Slack config.';
        this.saving = false;
      },
    });
  }
}
