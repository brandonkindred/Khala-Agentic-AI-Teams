import { Component, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, RouterLink } from '@angular/router';
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
import { HasUnsavedChanges } from '../../core/unsaved-changes.guard';
import { NotificationService } from '../../core/notification.service';
import { extractErrorDetail } from '../../shared/extract-error-detail';
import { InlineBannerComponent } from '../../shared/inline-banner/inline-banner.component';
import type {
  GitHubConfigResponse,
  GitHubConfigUpdate,
  GoogleBrowserLoginCredentialsBody,
  MediumConfigResponse,
  MediumConfigUpdate,
  MediumOAuthProvider,
  SlackConfigResponse,
  SlackConfigUpdate,
  SlackMode,
  TradingViewConfigResponse,
  TradingViewConfigUpdate,
  TradingViewTestResponse,
} from '../../models/integrations.model';

const SLACK_WEBHOOK_PREFIX = 'https://hooks.slack.com/';

type IntegrationKey = 'google' | 'slack' | 'medium' | 'github' | 'tradingview';

const INTEGRATION_KEYS: readonly IntegrationKey[] = ['google', 'slack', 'medium', 'github', 'tradingview'];

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
    InlineBannerComponent,
    RouterLink,
  ],
  templateUrl: './integrations-dashboard.component.html',
  styleUrl: './integrations-dashboard.component.scss',
})
export class IntegrationsDashboardComponent implements OnInit, HasUnsavedChanges {
  private readonly api = inject(IntegrationsApiService);
  private readonly route = inject(ActivatedRoute);
  private readonly notifications = inject(NotificationService);

  loadingSlack = false;
  saving = false;
  connecting = false;
  disconnecting = false;
  error: string | null = null;

  /**
   * Whether an unsaved secret has been typed into any credential field (drives
   * the CanDeactivate guard). Client secrets, tokens, and passwords are never
   * returned by the API, so a half-typed one lost to navigation must be
   * re-fetched from the external provider — the highest-stakes input here.
   *
   * Preconditions: none.
   * Postconditions: true while any save is in flight, or while any write-only
   * secret field holds text; false otherwise. The Slack webhook URL counts: it
   * embeds a secret token and is never returned by the API.
   */
  hasUnsavedChanges(): boolean {
    if (
      this.saving ||
      this.mediumSaving ||
      this.savingGoogleBrowserCredentials ||
      this.githubSaving ||
      this.tradingViewSaving
    ) {
      return true;
    }
    return !!(
      this.clientSecret.trim() ||
      this.botToken.trim() ||
      this.webhookUrl.trim() ||
      this.signingSecret.trim() ||
      this.googleAccountPassword.length > 0 ||
      this.githubPat.trim() ||
      this.githubWebhookSecret.trim() ||
      this.tradingViewToken.trim()
    );
  }

  /** Which integration card is currently expanded inline. Only one at a time. */
  expanded: IntegrationKey | null = null;

  /**
   * Card deep-linked via `?focus=<key>` — highlighted with an accent ring and
   * accompanied by the "came from…" context banner until the user interacts with it.
   * Null when the page was opened without a focus param (or after the user acts).
   */
  focusedKey: IntegrationKey | null = null;

  toggleExpanded(key: IntegrationKey): void {
    this.expanded = this.expanded === key ? null : key;
    // Any deliberate interaction clears the deep-link highlight/banner.
    this.focusedKey = null;
  }

  /**
   * Expand a card, scroll it into view, and ring it in response to `?focus=<key>`.
   *
   * Preconditions: `key` is a valid integration key.
   * Postconditions: `expanded` and `focusedKey` are set to `key`; the matching
   *   card is scrolled into view on the next frame (no-op where `scrollIntoView`
   *   is unavailable, e.g. jsdom).
   */
  private focusIntegration(key: IntegrationKey): void {
    this.expanded = key;
    this.focusedKey = key;
    setTimeout(() => {
      document.getElementById(`integration-${key}`)?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
    });
  }

  readonly totalIntegrations = 5;

  get connectedCount(): number {
    let n = 0;
    if (this.googleBrowserLoginConfigured) n += 1;
    if (this.oauthConnected) n += 1;
    if (this.mediumReadyForStats) n += 1;
    // The PAT alone defines repository access — a stored token means connected.
    if (this.githubTokenConfigured) n += 1;
    if (this.tradingViewEnabled && !!this.tradingViewServerUrl) n += 1;
    return n;
  }

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
  // Signing secret for inbound Slack events / slash commands (HMAC verification). The
  // value is write-only (never returned by the API); `signingSecretConfigured` reflects
  // whether one is stored so the input can show "Saved" without exposing the secret.
  signingSecret = '';
  signingSecretConfigured = false;

  ngOnInit(): void {
    this.loadGoogleBrowserLoginStatus();
    this.loadSlackConfig();
    this.loadMediumConfig();
    this.loadGitHubConfig();
    this.loadTradingViewConfig();
    this.handleOAuthCallback();
  }

  /** Read OAuth return query params from Slack and Medium Google flows. */
  private handleOAuthCallback(): void {
    this.route.queryParams.subscribe((params) => {
      const focus = params['focus'];
      if (typeof focus === 'string' && (INTEGRATION_KEYS as readonly string[]).includes(focus)) {
        // Deep link (e.g. from the Strategy Lab): open the named card in focus.
        this.focusIntegration(focus as IntegrationKey);
      }
      if (params['slack_connected']) {
        const team = params['team'] ? decodeURIComponent(params['team']) : null;
        this.notifications.saved(
          team ? `Connected to "${team}" workspace successfully.` : 'Slack connected successfully.',
        );
        this.expanded = 'slack';
        this.loadSlackConfig();
      } else if (params['slack_error']) {
        const errCode = params['slack_error'];
        this.error = this.friendlySlackOAuthError(errCode);
        this.expanded = 'slack';
      }
      if (params['medium_google_connected']) {
        this.notifications.saved('Google account linked for Medium workflow.');
        this.expanded = 'medium';
        this.loadMediumConfig();
      }
      if (params['medium_error']) {
        this.mediumError = this.friendlyMediumOAuthError(String(params['medium_error']));
        this.expanded = 'medium';
      }
    });
  }

  private friendlySlackOAuthError(code: string): string {
    const map: Record<string, string> = {
      access_denied: 'You cancelled the Slack authorization.',
      missing_code_or_state: 'Invalid OAuth response from Slack.',
      invalid_state: 'OAuth session expired or was tampered with. Please try again.',
      token_exchange_failed: 'Failed to exchange the authorization code. Check server logs.',
      missing_credentials: 'App credentials were not found. Please re-enter your Client ID and Secret.',
    };
    return map[code] ?? `Slack OAuth error: ${code}`;
  }

  private friendlyMediumOAuthError(code: string): string {
    const map: Record<string, string> = {
      access_denied: 'You cancelled the Google authorization.',
      missing_code_or_state: 'Invalid OAuth response from Google.',
      invalid_state: 'OAuth session expired or was tampered with. Please try again.',
      token_exchange_failed: 'Failed to exchange the authorization code. Check server logs.',
      missing_credentials: 'Google OAuth app credentials were not found. Save Client ID and Secret first.',
    };
    return map[code] ?? `Medium Google link error: ${code}`;
  }

  // ---------------------------------------------------------------------------
  // Shared Google / Gmail (Playwright — any integration with “Sign in with Google”)
  // ---------------------------------------------------------------------------

  googleBrowserLoading = false;
  googleBrowserError: string | null = null;
  googleBrowserLoginConfigured = false;
  /** When false, API runs without Postgres — browser-login credentials are not supported. */
  googleBrowserStorageAvailable = true;
  googleAccountEmail = '';
  googleAccountPassword = '';
  savingGoogleBrowserCredentials = false;
  clearingGoogleBrowserCredentials = false;

  loadGoogleBrowserLoginStatus(): void {
    this.googleBrowserLoading = true;
    this.googleBrowserError = null;
    this.api.getGoogleBrowserLoginStatus().subscribe({
      next: (r) => {
        this.googleBrowserLoginConfigured = r.configured;
        // Older APIs omit this field; treat as available so we do not disable the form incorrectly.
        this.googleBrowserStorageAvailable = r.storage_available !== false;
        this.googleBrowserLoading = false;
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.googleBrowserError =
          extractErrorDetail(err, 'Failed to load Google browser-login status.', { joinValidationArray: true });
        this.googleBrowserLoading = false;
      },
    });
  }

  saveGoogleBrowserLoginCredentials(): void {
    this.savingGoogleBrowserCredentials = true;
    this.googleBrowserError = null;
    const body: GoogleBrowserLoginCredentialsBody = {
      email: this.googleAccountEmail.trim(),
      password: this.googleAccountPassword,
    };
    this.api.putGoogleBrowserLoginCredentials(body).subscribe({
      next: (r) => {
        this.googleBrowserLoginConfigured = r.configured;
        this.googleBrowserStorageAvailable = r.storage_available !== false;
        this.googleAccountPassword = '';
        this.notifications.saved('Gmail / Google credentials saved (encrypted on the server).');
        this.savingGoogleBrowserCredentials = false;
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.googleBrowserError =
          extractErrorDetail(err, 'Failed to save Google credentials.', { joinValidationArray: true });
        this.savingGoogleBrowserCredentials = false;
      },
    });
  }

  clearGoogleBrowserLoginCredentials(): void {
    this.clearingGoogleBrowserCredentials = true;
    this.googleBrowserError = null;
    this.api.deleteGoogleBrowserLoginCredentials().subscribe({
      next: (r) => {
        this.googleBrowserLoginConfigured = r.configured;
        this.googleBrowserStorageAvailable = r.storage_available !== false;
        this.googleAccountEmail = '';
        this.googleAccountPassword = '';
        this.notifications.saved('Shared Google credentials removed.');
        this.clearingGoogleBrowserCredentials = false;
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.googleBrowserError =
          extractErrorDetail(err, 'Failed to clear Google credentials.', { joinValidationArray: true });
        this.clearingGoogleBrowserCredentials = false;
      },
    });
  }

  // ---------------------------------------------------------------------------
  // Medium.com
  // ---------------------------------------------------------------------------

  mediumLoading = false;
  mediumSaving = false;
  mediumError: string | null = null;

  mediumEnabled = false;
  mediumProvider: MediumOAuthProvider = 'google';
  mediumSessionConfigured = false;

  get mediumIdentityReady(): boolean {
    return (
      this.mediumProvider !== 'google' ||
      this.mediumSessionConfigured ||
      this.googleBrowserLoginConfigured
    );
  }

  mediumBrowserLoginRunning = false;

  get mediumReadyForStats(): boolean {
    return (
      this.mediumEnabled &&
      this.mediumIdentityReady &&
      (this.mediumSessionConfigured || this.googleBrowserLoginConfigured)
    );
  }

  get mediumProviderLabel(): string {
    const labels: Record<MediumOAuthProvider, string> = {
      google: 'Google',
      apple: 'Apple',
      facebook: 'Facebook',
      twitter: 'X (Twitter)',
    };
    return labels[this.mediumProvider] ?? this.mediumProvider;
  }

  loadMediumConfig(): void {
    this.mediumLoading = true;
    this.api.getMediumConfig().subscribe({
      next: (res: MediumConfigResponse) => {
        this.applyMediumConfig(res);
        this.mediumLoading = false;
      },
      error: (err) => {
        this.mediumError = extractErrorDetail(err, 'Failed to load Medium config', { joinValidationArray: true });
        this.mediumLoading = false;
      },
    });
  }

  private applyMediumConfig(res: MediumConfigResponse): void {
    this.mediumEnabled = res.enabled;
    this.mediumProvider = res.oauth_provider;
    this.mediumSessionConfigured = res.session_configured;
  }

  saveMediumSettings(): void {
    this.mediumSaving = true;
    this.mediumError = null;
    const body: MediumConfigUpdate = {
      enabled: this.mediumEnabled,
      oauth_provider: this.mediumProvider,
      google_client_id: '',
      google_client_secret: '',
    };
    this.api.updateMediumConfig(body).subscribe({
      next: (res) => {
        this.applyMediumConfig(res);
        this.notifications.saved('Medium integration saved.');
        this.mediumSaving = false;
      },
      error: (err) => {
        this.mediumError = extractErrorDetail(err, 'Failed to save Medium settings.', { joinValidationArray: true });
        this.mediumSaving = false;
      },
    });
  }

  runMediumBrowserLogin(): void {
    this.mediumBrowserLoginRunning = true;
    this.mediumError = null;
    this.api.mediumBrowserLoginSession().subscribe({
      next: (res: MediumConfigResponse) => {
        this.applyMediumConfig(res);
        this.notifications.saved('Medium browser session saved using shared Google credentials from Integrations.');
        this.mediumBrowserLoginRunning = false;
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.mediumError =
          extractErrorDetail(err, 'Automated Medium browser login failed.', { joinValidationArray: true });
        this.mediumBrowserLoginRunning = false;
      },
    });
  }

  loadSlackConfig(): void {
    this.loadingSlack = true;
    this.api.getSlackConfig().subscribe({
      next: (res: SlackConfigResponse) => {
        this.applyConfig(res);
        this.loadingSlack = false;
      },
      error: (err) => {
        this.error = extractErrorDetail(err, 'Failed to load Slack config', { joinValidationArray: true });
        this.loadingSlack = false;
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
    this.signingSecretConfigured = res.signing_secret_configured ?? false;
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
    this.signingSecret = '';
  }

  // ---------------------------------------------------------------------------
  // OAuth flow
  // ---------------------------------------------------------------------------

  connectWithSlack(): void {
    this.connecting = true;
    this.error = null;

    const clientId = this.clientId.trim();
    const clientSecret = this.clientSecret.trim();

    const doConnect = () => {
      this.api.getSlackOAuthUrl().subscribe({
        next: (res) => {
          window.location.href = res.url;
        },
        error: (err) => {
          this.error = extractErrorDetail(err, 'Failed to start Slack OAuth.', { joinValidationArray: true });
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
        signing_secret: '',
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
          this.error = extractErrorDetail(err, 'Failed to save credentials.', { joinValidationArray: true });
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
    this.api.disconnectSlack().subscribe({
      next: (res) => {
        this.applyConfig(res);
        this.notifications.saved('Slack disconnected.');
        this.disconnecting = false;
      },
      error: (err) => {
        this.error = extractErrorDetail(err, 'Failed to disconnect Slack.', { joinValidationArray: true });
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

    const body: SlackConfigUpdate = {
      enabled: this.slackEnabled,
      mode: this.mode,
      client_id: '',
      client_secret: '',
      signing_secret: this.signingSecret.trim(),
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
        this.notifications.saved('Settings saved.');
        this.saving = false;
      },
      error: (err) => {
        this.error = extractErrorDetail(err, 'Failed to save settings.', { joinValidationArray: true });
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

    const body: SlackConfigUpdate = {
      enabled: this.slackEnabled,
      mode: this.mode,
      client_id: this.clientId.trim(),
      client_secret: this.clientSecret.trim(),
      signing_secret: this.signingSecret.trim(),
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
        this.notifications.saved('Slack integration saved.');
        this.saving = false;
      },
      error: (err) => {
        this.error = extractErrorDetail(err, 'Failed to save Slack config.', { joinValidationArray: true });
        this.saving = false;
      },
    });
  }

  // ---------------------------------------------------------------------------
  // GitHub
  // ---------------------------------------------------------------------------

  githubLoading = false;
  githubSaving = false;
  githubDisconnecting = false;
  githubError: string | null = null;

  githubEnabled = false;
  githubPat = '';
  githubDefaultLabel = '';
  githubTokenConfigured = false;
  // Webhook signing secret for the "@khala review" PR-comment trigger. The value is
  // write-only (never returned by the API); `githubWebhookSecretConfigured` reflects
  // whether one is stored so the input can show "Saved" without exposing the secret.
  githubWebhookSecret = '';
  githubWebhookSecretConfigured = false;
  // True when the PAT store (Postgres) is configured but unreachable, so the panel
  // can warn that the integration is down rather than implying it was never set up.
  githubStoreUnreachable = false;

  loadGitHubConfig(): void {
    this.githubLoading = true;
    this.githubError = null;
    this.api.getGitHubConfig().subscribe({
      next: (res: GitHubConfigResponse) => {
        this.githubEnabled = res.enabled;
        this.githubDefaultLabel = res.default_label;
        this.githubTokenConfigured = res.token_configured;
        this.githubStoreUnreachable = res.credential_store_unreachable ?? false;
        this.githubWebhookSecretConfigured = res.webhook_secret_configured ?? false;
        this.githubPat = '';
        this.githubWebhookSecret = '';
        this.githubLoading = false;
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.githubError = extractErrorDetail(err, 'Failed to load GitHub config.', { joinValidationArray: true });
        this.githubLoading = false;
        // Current store state is unknown after a failed reload — clear the stale
        // unreachable flag so a banner from a prior load doesn't linger.
        this.githubStoreUnreachable = false;
      },
    });
  }

  saveGitHubConfig(): void {
    this.githubSaving = true;
    this.githubError = null;
    // No repository list is sent: the PAT's own authorization configuration decides
    // which repositories the integration can reach.
    const body: GitHubConfigUpdate = {
      enabled: this.githubEnabled,
      token: this.githubPat,
      default_label: this.githubDefaultLabel.trim(),
      repo_path: '',
      // Empty preserves the existing stored secret (mirrors the token field).
      webhook_secret: this.githubWebhookSecret,
    };
    this.api.updateGitHubConfig(body).subscribe({
      next: (res: GitHubConfigResponse) => {
        this.githubEnabled = res.enabled;
        this.githubDefaultLabel = res.default_label;
        this.githubTokenConfigured = res.token_configured;
        this.githubWebhookSecretConfigured = res.webhook_secret_configured ?? false;
        this.githubPat = '';
        this.githubWebhookSecret = '';
        this.notifications.saved('GitHub integration saved.');
        this.githubSaving = false;
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.githubError = extractErrorDetail(err, 'Failed to save GitHub config.', { joinValidationArray: true });
        this.githubSaving = false;
      },
    });
  }

  disconnectGitHub(): void {
    this.githubDisconnecting = true;
    this.githubError = null;
    this.api.deleteGitHubConfig().subscribe({
      next: (res: GitHubConfigResponse) => {
        this.githubEnabled = res.enabled;
        this.githubDefaultLabel = res.default_label;
        this.githubTokenConfigured = res.token_configured;
        this.githubWebhookSecretConfigured = res.webhook_secret_configured ?? false;
        this.githubPat = '';
        this.githubWebhookSecret = '';
        this.notifications.saved('GitHub disconnected.');
        this.githubDisconnecting = false;
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.githubError = extractErrorDetail(err, 'Failed to disconnect GitHub.', { joinValidationArray: true });
        this.githubDisconnecting = false;
      },
    });
  }

  // ---------------------------------------------------------------------------
  // TradingView (MCP market-data source for the Strategy Lab)
  // ---------------------------------------------------------------------------

  tradingViewLoading = false;
  tradingViewSaving = false;
  tradingViewDisconnecting = false;
  tradingViewError: string | null = null;

  tradingViewEnabled = false;
  tradingViewServerUrl = '';
  tradingViewToolName = '';
  // Write-only; the token is never returned by the API. `tradingViewTokenConfigured`
  // reflects whether one is stored so the field can show "Saved" without exposing it.
  tradingViewToken = '';
  tradingViewTokenConfigured = false;

  // Live "test connection" state — the result of the last reachability probe.
  tradingViewTesting = false;
  tradingViewTestResult: TradingViewTestResponse | null = null;

  serverUrlInvalid(): boolean {
    const u = (this.tradingViewServerUrl || '').trim();
    if (!u) return false;
    return !u.startsWith('http://') && !u.startsWith('https://');
  }

  private applyTradingViewConfig(res: TradingViewConfigResponse): void {
    this.tradingViewEnabled = res.enabled;
    this.tradingViewServerUrl = res.mcp_server_url;
    this.tradingViewToolName = res.tool_name;
    this.tradingViewTokenConfigured = res.auth_token_configured;
    // Never repopulate the secret from a response.
    this.tradingViewToken = '';
    // A prior probe result no longer describes the freshly-applied config.
    this.tradingViewTestResult = null;
  }

  /** True when a saved server URL exists to probe and no save/test is in flight. */
  canTestTradingView(): boolean {
    return !!this.tradingViewServerUrl.trim() && !this.serverUrlInvalid() && !this.tradingViewTesting && !this.tradingViewSaving;
  }

  /**
   * Probe the stored MCP server and surface a reachable / unreachable result.
   *
   * Preconditions: a server URL is saved (`canTestTradingView()` is true).
   * Postconditions: `tradingViewTestResult` holds the probe outcome; a transport
   *   failure is reported as `{ ok: false }` rather than thrown.
   */
  testTradingView(): void {
    this.tradingViewTesting = true;
    this.tradingViewTestResult = null;
    this.tradingViewError = null;
    this.api.testTradingViewConnection().subscribe({
      next: (res: TradingViewTestResponse) => {
        this.tradingViewTestResult = res;
        this.tradingViewTesting = false;
      },
      error: (err) => {
        this.tradingViewTestResult = {
          ok: false,
          detail: extractErrorDetail(err, 'Connection test failed.', { joinValidationArray: true }),
        };
        this.tradingViewTesting = false;
      },
    });
  }

  loadTradingViewConfig(): void {
    this.tradingViewLoading = true;
    this.tradingViewError = null;
    this.api.getTradingViewConfig().subscribe({
      next: (res: TradingViewConfigResponse) => {
        this.applyTradingViewConfig(res);
        this.tradingViewLoading = false;
      },
      error: (err) => {
        this.tradingViewError = extractErrorDetail(err, 'Failed to load TradingView config.', { joinValidationArray: true });
        this.tradingViewLoading = false;
      },
    });
  }

  saveTradingViewConfig(): void {
    const serverUrl = this.tradingViewServerUrl.trim();
    if (this.serverUrlInvalid()) {
      this.tradingViewError = 'Server URL must start with http:// or https://';
      return;
    }
    if (this.tradingViewEnabled && !serverUrl) {
      this.tradingViewError = 'Server URL is required to enable the TradingView integration.';
      return;
    }

    this.tradingViewSaving = true;
    this.tradingViewError = null;
    const body: TradingViewConfigUpdate = {
      enabled: this.tradingViewEnabled,
      mcp_server_url: serverUrl,
      tool_name: this.tradingViewToolName.trim(),
      auth_token: this.tradingViewToken,
    };
    this.api.updateTradingViewConfig(body).subscribe({
      next: (res: TradingViewConfigResponse) => {
        this.applyTradingViewConfig(res);
        this.notifications.saved('TradingView integration saved.');
        this.tradingViewSaving = false;
      },
      error: (err) => {
        this.tradingViewError = extractErrorDetail(err, 'Failed to save TradingView config.', { joinValidationArray: true });
        this.tradingViewSaving = false;
      },
    });
  }

  disconnectTradingView(): void {
    this.tradingViewDisconnecting = true;
    this.tradingViewError = null;
    this.api.deleteTradingViewConfig().subscribe({
      next: (res: TradingViewConfigResponse) => {
        this.applyTradingViewConfig(res);
        this.notifications.saved('TradingView disconnected.');
        this.tradingViewDisconnecting = false;
      },
      error: (err) => {
        this.tradingViewError = extractErrorDetail(err, 'Failed to disconnect TradingView.', { joinValidationArray: true });
        this.tradingViewDisconnecting = false;
      },
    });
  }
}
