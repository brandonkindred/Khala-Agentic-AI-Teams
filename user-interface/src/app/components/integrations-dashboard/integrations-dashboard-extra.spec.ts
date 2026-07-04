import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, Subject, throwError } from 'rxjs';
import { ActivatedRoute, provideRouter } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatSnackBar } from '@angular/material/snack-bar';
import { vi, beforeEach, afterEach } from 'vitest';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { IntegrationsDashboardComponent } from './integrations-dashboard.component';

interface ApiStub {
  getSlackConfig: ReturnType<typeof vi.fn>;
  updateSlackConfig: ReturnType<typeof vi.fn>;
  getGoogleBrowserLoginStatus: ReturnType<typeof vi.fn>;
  putGoogleBrowserLoginCredentials: ReturnType<typeof vi.fn>;
  deleteGoogleBrowserLoginCredentials: ReturnType<typeof vi.fn>;
  getMediumConfig: ReturnType<typeof vi.fn>;
  updateMediumConfig: ReturnType<typeof vi.fn>;
  mediumBrowserLoginSession: ReturnType<typeof vi.fn>;
  getSlackOAuthUrl: ReturnType<typeof vi.fn>;
  disconnectSlack: ReturnType<typeof vi.fn>;
  getGitHubConfig: ReturnType<typeof vi.fn>;
  getTradingViewConfig: ReturnType<typeof vi.fn>;
  updateTradingViewConfig: ReturnType<typeof vi.fn>;
  deleteTradingViewConfig: ReturnType<typeof vi.fn>;
}

describe('IntegrationsDashboardComponent (extra coverage)', () => {
  let api: ApiStub;
  let fixture: ComponentFixture<IntegrationsDashboardComponent>;
  let component: IntegrationsDashboardComponent;
  let queryParams$: Subject<Record<string, string>>;
  let originalLocation: Location;
  let snackBar: { open: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    queryParams$ = new Subject();
    snackBar = { open: vi.fn() };
    api = {
      getSlackConfig: vi.fn().mockReturnValue(of({
        enabled: false,
        webhook_configured: false,
        bot_token_configured: false,
        channel_display_name: '',
        default_channel: '',
        notify_open_questions: true,
        notify_pa_responses: true,
      })),
      updateSlackConfig: vi.fn().mockReturnValue(of({
        enabled: true,
        webhook_configured: true,
        bot_token_configured: false,
        channel_display_name: '',
        default_channel: '',
        notify_open_questions: true,
        notify_pa_responses: true,
      })),
      getGoogleBrowserLoginStatus: vi.fn().mockReturnValue(of({ configured: false, storage_available: true })),
      putGoogleBrowserLoginCredentials: vi.fn().mockReturnValue(of({ configured: true, storage_available: true })),
      deleteGoogleBrowserLoginCredentials: vi.fn().mockReturnValue(of({ configured: false, storage_available: true })),
      getMediumConfig: vi.fn().mockReturnValue(of({ enabled: false, oauth_provider: 'google', session_configured: false })),
      updateMediumConfig: vi.fn().mockReturnValue(of({ enabled: true, oauth_provider: 'google', session_configured: true })),
      mediumBrowserLoginSession: vi.fn().mockReturnValue(of({ enabled: true, oauth_provider: 'google', session_configured: true })),
      getSlackOAuthUrl: vi.fn().mockReturnValue(of({ url: 'https://slack.com/oauth' })),
      getGitHubConfig: vi.fn().mockReturnValue(of({ enabled: false, token_configured: false, owner: '', repo: '', default_label: '' })),
      getTradingViewConfig: vi.fn().mockReturnValue(of({ enabled: false, mcp_server_url: '', tool_name: 'get_ohlcv', auth_token_configured: false })),
      updateTradingViewConfig: vi.fn().mockReturnValue(of({ enabled: true, mcp_server_url: 'https://tv/mcp', tool_name: 'get_ohlcv', auth_token_configured: true })),
      deleteTradingViewConfig: vi.fn().mockReturnValue(of({ enabled: false, mcp_server_url: '', tool_name: '', auth_token_configured: false })),
      disconnectSlack: vi.fn().mockReturnValue(of({
        enabled: false,
        webhook_configured: false,
        bot_token_configured: false,
        channel_display_name: '',
        default_channel: '',
        notify_open_questions: true,
        notify_pa_responses: true,
      })),
    };

    originalLocation = window.location;
    Object.defineProperty(window, 'location', {
      value: { href: '', assign: vi.fn(), origin: 'http://localhost' },
      writable: true,
      configurable: true,
    });

    await TestBed.configureTestingModule({
      imports: [IntegrationsDashboardComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: IntegrationsApiService, useValue: api },
        { provide: ActivatedRoute, useValue: { queryParams: queryParams$.asObservable() } },
        { provide: MatSnackBar, useValue: snackBar },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(IntegrationsDashboardComponent);
    component = fixture.componentInstance;
  });

  afterEach(() => {
    Object.defineProperty(window, 'location', { value: originalLocation, writable: true, configurable: true });
    TestBed.resetTestingModule();
  });

  // ---------------------------------------------------------------------
  // expansion
  // ---------------------------------------------------------------------

  it('toggleExpanded opens and closes the same key, switches between keys', () => {
    fixture.detectChanges();
    expect(component.expanded).toBeNull();
    component.toggleExpanded('google');
    expect(component.expanded).toBe('google');
    component.toggleExpanded('google');
    expect(component.expanded).toBeNull();
    component.toggleExpanded('slack');
    component.toggleExpanded('medium');
    expect(component.expanded).toBe('medium');
  });

  it('connectedCount counts configured integrations', () => {
    fixture.detectChanges();
    expect(component.connectedCount).toBe(0);
    component.googleBrowserLoginConfigured = true;
    component.oauthConnected = true;
    component.mediumEnabled = true;
    component.mediumSessionConfigured = true;
    expect(component.connectedCount).toBe(3);
  });

  // ---------------------------------------------------------------------
  // OAuth callback query params
  // ---------------------------------------------------------------------

  it('handles slack_connected with team name', () => {
    fixture.detectChanges();
    queryParams$.next({ slack_connected: '1', team: encodeURIComponent('Foo Team') });
    expect(snackBar.open).toHaveBeenCalledWith(expect.stringContaining('"Foo Team"'), 'Dismiss', { duration: 3000 });
    expect(component.expanded).toBe('slack');
  });

  it('handles slack_connected without team', () => {
    fixture.detectChanges();
    queryParams$.next({ slack_connected: '1' });
    expect(snackBar.open).toHaveBeenCalledWith('Slack connected successfully.', 'Dismiss', { duration: 3000 });
  });

  it('handles slack_error with known and unknown codes', () => {
    fixture.detectChanges();
    queryParams$.next({ slack_error: 'access_denied' });
    expect(component.error).toContain('cancelled');
    queryParams$.next({ slack_error: 'missing_code_or_state' });
    expect(component.error).toContain('Invalid OAuth response');
    queryParams$.next({ slack_error: 'invalid_state' });
    expect(component.error).toContain('expired');
    queryParams$.next({ slack_error: 'token_exchange_failed' });
    expect(component.error).toContain('Failed to exchange');
    queryParams$.next({ slack_error: 'missing_credentials' });
    expect(component.error).toContain('App credentials');
    queryParams$.next({ slack_error: 'unknown_code' });
    expect(component.error).toContain('unknown_code');
  });

  it('handles medium_google_connected and medium_error', () => {
    fixture.detectChanges();
    queryParams$.next({ medium_google_connected: '1' });
    expect(snackBar.open).toHaveBeenCalledWith(expect.stringContaining('linked'), 'Dismiss', { duration: 3000 });
    expect(component.expanded).toBe('medium');

    queryParams$.next({ medium_error: 'access_denied' });
    expect(component.mediumError).toContain('cancelled');
    queryParams$.next({ medium_error: 'missing_code_or_state' });
    expect(component.mediumError).toContain('Invalid OAuth');
    queryParams$.next({ medium_error: 'invalid_state' });
    expect(component.mediumError).toContain('expired');
    queryParams$.next({ medium_error: 'token_exchange_failed' });
    expect(component.mediumError).toContain('Failed to exchange');
    queryParams$.next({ medium_error: 'missing_credentials' });
    expect(component.mediumError).toContain('Google OAuth app credentials');
    queryParams$.next({ medium_error: 'unknown' });
    expect(component.mediumError).toContain('unknown');
  });

  // ---------------------------------------------------------------------
  // Google browser-login credentials
  // ---------------------------------------------------------------------

  it('loadGoogleBrowserLoginStatus handles success and error', () => {
    fixture.detectChanges();
    expect(component.googleBrowserLoginConfigured).toBe(false);
    expect(component.googleBrowserStorageAvailable).toBe(true);

    api.getGoogleBrowserLoginStatus.mockReturnValue(throwError(() => ({ error: { detail: 'oh no' } })));
    component.loadGoogleBrowserLoginStatus();
    expect(component.googleBrowserError).toBe('oh no');
  });

  it('saveGoogleBrowserLoginCredentials saves and clears password', () => {
    fixture.detectChanges();
    component.googleAccountEmail = 'me@example.com';
    component.googleAccountPassword = 'secret';
    component.saveGoogleBrowserLoginCredentials();
    expect(api.putGoogleBrowserLoginCredentials).toHaveBeenCalledWith({ email: 'me@example.com', password: 'secret' });
    expect(component.googleAccountPassword).toBe('');
    expect(snackBar.open).toHaveBeenCalledWith(expect.stringContaining('saved'), 'Dismiss', { duration: 3000 });
  });

  it('saveGoogleBrowserLoginCredentials sets error on failure', () => {
    fixture.detectChanges();
    api.putGoogleBrowserLoginCredentials.mockReturnValue(throwError(() => ({ message: 'put failed' })));
    component.saveGoogleBrowserLoginCredentials();
    expect(component.googleBrowserError).toBe('put failed');
  });

  it('clearGoogleBrowserLoginCredentials clears values', () => {
    fixture.detectChanges();
    component.googleAccountEmail = 'a@b';
    component.googleAccountPassword = 'x';
    component.clearGoogleBrowserLoginCredentials();
    expect(api.deleteGoogleBrowserLoginCredentials).toHaveBeenCalled();
    expect(component.googleAccountEmail).toBe('');
    expect(component.googleAccountPassword).toBe('');
    expect(snackBar.open).toHaveBeenCalledWith(expect.stringContaining('removed'), 'Dismiss', { duration: 3000 });
  });

  it('clearGoogleBrowserLoginCredentials handles error', () => {
    fixture.detectChanges();
    api.deleteGoogleBrowserLoginCredentials.mockReturnValue(throwError(() => ({ message: 'del fail' })));
    component.clearGoogleBrowserLoginCredentials();
    expect(component.googleBrowserError).toBe('del fail');
  });

  // ---------------------------------------------------------------------
  // Medium
  // ---------------------------------------------------------------------

  it('loadMediumConfig handles success and error', () => {
    fixture.detectChanges();
    expect(api.getMediumConfig).toHaveBeenCalled();
    expect(component.mediumEnabled).toBe(false);
    expect(component.mediumProvider).toBe('google');
    expect(component.mediumSessionConfigured).toBe(false);

    api.getMediumConfig.mockReturnValue(throwError(() => ({ error: { detail: 'medium fail' } })));
    component.loadMediumConfig();
    expect(component.mediumError).toBe('medium fail');
  });

  it('mediumIdentityReady and mediumReadyForStats reflect configuration', () => {
    fixture.detectChanges();
    component.mediumProvider = 'google';
    component.mediumSessionConfigured = false;
    component.googleBrowserLoginConfigured = false;
    expect(component.mediumIdentityReady).toBe(false);

    component.googleBrowserLoginConfigured = true;
    expect(component.mediumIdentityReady).toBe(true);

    component.mediumProvider = 'apple';
    expect(component.mediumIdentityReady).toBe(true);

    component.mediumProvider = 'google';
    component.mediumEnabled = true;
    component.mediumSessionConfigured = true;
    expect(component.mediumReadyForStats).toBe(true);
  });

  it('mediumProviderLabel maps known providers', () => {
    fixture.detectChanges();
    component.mediumProvider = 'google';
    expect(component.mediumProviderLabel).toBe('Google');
    component.mediumProvider = 'apple';
    expect(component.mediumProviderLabel).toBe('Apple');
    component.mediumProvider = 'facebook';
    expect(component.mediumProviderLabel).toBe('Facebook');
    component.mediumProvider = 'twitter';
    expect(component.mediumProviderLabel).toBe('X (Twitter)');
    (component as unknown as { mediumProvider: string }).mediumProvider = 'unknown';
    expect(component.mediumProviderLabel).toBe('unknown');
  });

  it('saveMediumSettings posts config and handles error', () => {
    fixture.detectChanges();
    component.mediumEnabled = true;
    component.mediumProvider = 'google';
    component.saveMediumSettings();
    expect(api.updateMediumConfig).toHaveBeenCalled();
    expect(snackBar.open).toHaveBeenCalledWith(expect.stringContaining('saved'), 'Dismiss', { duration: 3000 });

    api.updateMediumConfig.mockReturnValue(throwError(() => ({ message: 'medium-save-fail' })));
    component.saveMediumSettings();
    expect(component.mediumError).toBe('medium-save-fail');
  });

  it('runMediumBrowserLogin handles success and error', () => {
    fixture.detectChanges();
    component.runMediumBrowserLogin();
    expect(api.mediumBrowserLoginSession).toHaveBeenCalled();
    expect(snackBar.open).toHaveBeenCalledWith(expect.stringContaining('browser session'), 'Dismiss', { duration: 3000 });

    api.mediumBrowserLoginSession.mockReturnValue(throwError(() => ({ message: 'browser-fail' })));
    component.runMediumBrowserLogin();
    expect(component.mediumError).toBe('browser-fail');
  });

  // ---------------------------------------------------------------------
  // Slack save + advanced + connect/disconnect
  // ---------------------------------------------------------------------

  it('saveSettings posts settings and handles success/error', () => {
    fixture.detectChanges();
    component.slackEnabled = true;
    component.defaultChannel = ' #ops ';
    component.saveSettings();
    expect(api.updateSlackConfig).toHaveBeenCalledWith(expect.objectContaining({
      enabled: true,
      default_channel: '#ops',
    }));
    expect(snackBar.open).toHaveBeenCalledWith('Settings saved.', 'Dismiss', { duration: 3000 });

    api.updateSlackConfig.mockReturnValue(throwError(() => ({ message: 'oops' })));
    component.saveSettings();
    expect(component.error).toBe('oops');
  });

  it('saveAdvanced requires webhook for webhook mode', () => {
    fixture.detectChanges();
    component.slackEnabled = true;
    component.mode = 'webhook';
    component.webhookConfigured = false;
    component.webhookUrl = '';
    component.saveAdvanced();
    expect(component.error).toContain('Webhook URL is required');
  });

  it('saveAdvanced succeeds in webhook mode with valid URL', () => {
    fixture.detectChanges();
    component.slackEnabled = true;
    component.mode = 'webhook';
    component.webhookUrl = 'https://hooks.slack.com/services/T0/B0/' + 'x'.repeat(40);
    component.saveAdvanced();
    expect(api.updateSlackConfig).toHaveBeenCalled();
  });

  it('saveAdvanced requires bot token + default channel for bot mode', () => {
    fixture.detectChanges();
    component.slackEnabled = true;
    component.mode = 'bot';
    component.botTokenConfigured = false;
    component.botToken = '';
    component.saveAdvanced();
    expect(component.error).toContain('Bot token is required');

    component.botToken = 'invalid-format';
    component.saveAdvanced();
    expect(component.error).toContain('xoxb-');

    component.botToken = 'xoxb-valid';
    component.defaultChannel = '';
    component.saveAdvanced();
    expect(component.error).toContain('Default channel');
  });

  it('saveAdvanced succeeds in bot mode with valid token + channel', () => {
    fixture.detectChanges();
    component.slackEnabled = true;
    component.mode = 'bot';
    component.botToken = 'xoxb-validtoken';
    component.defaultChannel = '#ops';
    component.saveAdvanced();
    expect(api.updateSlackConfig).toHaveBeenCalled();
  });

  it('saveAdvanced uses configured webhook when none provided', () => {
    fixture.detectChanges();
    component.slackEnabled = true;
    component.mode = 'webhook';
    component.webhookConfigured = true;
    component.webhookUrl = '';
    component.saveAdvanced();
    expect(api.updateSlackConfig).toHaveBeenCalled();
  });

  it('botTokenInvalid and webhookUrlInvalid edge cases', () => {
    fixture.detectChanges();
    component.botToken = '';
    expect(component.botTokenInvalid()).toBe(false);
    component.botToken = 'xoxb-valid';
    expect(component.botTokenInvalid()).toBe(false);
    component.botToken = 'invalid';
    expect(component.botTokenInvalid()).toBe(true);
  });

  it('connectWithSlack with credentials saves then redirects', () => {
    fixture.detectChanges();
    component.clientId = 'cid';
    component.clientSecret = 'sec';
    component.connectWithSlack();
    expect(api.updateSlackConfig).toHaveBeenCalled();
    expect(window.location.href).toBe('https://slack.com/oauth');
  });

  it('connectWithSlack without credentials skips save and redirects', () => {
    fixture.detectChanges();
    component.clientId = '';
    component.clientSecret = '';
    component.connectWithSlack();
    expect(api.updateSlackConfig).not.toHaveBeenCalled();
    expect(window.location.href).toBe('https://slack.com/oauth');
  });

  it('connectWithSlack handles updateSlackConfig error', () => {
    fixture.detectChanges();
    component.clientId = 'cid';
    api.updateSlackConfig.mockReturnValue(throwError(() => ({ message: 'save fail' })));
    component.connectWithSlack();
    expect(component.error).toBe('save fail');
  });

  it('connectWithSlack handles oauth URL error', () => {
    fixture.detectChanges();
    api.getSlackOAuthUrl.mockReturnValue(throwError(() => ({ message: 'oauth fail' })));
    component.connectWithSlack();
    expect(component.error).toBe('oauth fail');
  });

  it('disconnectSlack handles success and error', () => {
    fixture.detectChanges();
    component.disconnectSlack();
    expect(api.disconnectSlack).toHaveBeenCalled();
    expect(snackBar.open).toHaveBeenCalledWith('Slack disconnected.', 'Dismiss', { duration: 3000 });

    api.disconnectSlack.mockReturnValue(throwError(() => ({ message: 'disconnect fail' })));
    component.disconnectSlack();
    expect(component.error).toBe('disconnect fail');
  });

  // -------------------------------------------------------------------------
  // TradingView
  // -------------------------------------------------------------------------

  it('loadTradingViewConfig applies config on ngOnInit', () => {
    api.getTradingViewConfig.mockReturnValue(
      of({ enabled: true, mcp_server_url: 'https://tv/mcp', tool_name: 'fb', auth_token_configured: true }),
    );
    fixture.detectChanges();
    expect(component.tradingViewEnabled).toBe(true);
    expect(component.tradingViewServerUrl).toBe('https://tv/mcp');
    expect(component.tradingViewToolName).toBe('fb');
    expect(component.tradingViewTokenConfigured).toBe(true);
    expect(component.tradingViewToken).toBe('');
  });

  it('loadTradingViewConfig handles error', () => {
    api.getTradingViewConfig.mockReturnValue(throwError(() => ({ error: { detail: 'tv down' } })));
    fixture.detectChanges();
    expect(component.tradingViewError).toBe('tv down');
  });

  it('serverUrlInvalid flags non-http URLs only', () => {
    fixture.detectChanges();
    component.tradingViewServerUrl = '';
    expect(component.serverUrlInvalid()).toBe(false);
    component.tradingViewServerUrl = 'ftp://bad';
    expect(component.serverUrlInvalid()).toBe(true);
    component.tradingViewServerUrl = 'https://ok/mcp';
    expect(component.serverUrlInvalid()).toBe(false);
  });

  it('saveTradingViewConfig blocks an invalid URL', () => {
    fixture.detectChanges();
    component.tradingViewServerUrl = 'ftp://bad';
    component.saveTradingViewConfig();
    expect(component.tradingViewError).toContain('http');
    expect(api.updateTradingViewConfig).not.toHaveBeenCalled();
  });

  it('saveTradingViewConfig requires a URL when enabling', () => {
    fixture.detectChanges();
    component.tradingViewEnabled = true;
    component.tradingViewServerUrl = '';
    component.saveTradingViewConfig();
    expect(component.tradingViewError).toContain('required');
    expect(api.updateTradingViewConfig).not.toHaveBeenCalled();
  });

  it('saveTradingViewConfig posts and applies the response', () => {
    fixture.detectChanges();
    component.tradingViewEnabled = true;
    component.tradingViewServerUrl = 'https://tv/mcp';
    component.tradingViewToolName = '';
    component.tradingViewToken = 'secret';
    component.saveTradingViewConfig();
    expect(api.updateTradingViewConfig).toHaveBeenCalledWith({
      enabled: true,
      mcp_server_url: 'https://tv/mcp',
      tool_name: '',
      auth_token: 'secret',
    });
    expect(snackBar.open).toHaveBeenCalledWith('TradingView integration saved.', 'Dismiss', {
      duration: 3000,
    });
    expect(component.tradingViewTokenConfigured).toBe(true);
    expect(component.tradingViewToken).toBe('');
  });

  it('saveTradingViewConfig surfaces a save error', () => {
    fixture.detectChanges();
    api.updateTradingViewConfig.mockReturnValue(throwError(() => ({ message: 'save fail' })));
    component.tradingViewServerUrl = 'https://tv/mcp';
    component.saveTradingViewConfig();
    expect(component.tradingViewError).toBe('save fail');
  });

  it('disconnectTradingView clears config and handles error', () => {
    fixture.detectChanges();
    component.disconnectTradingView();
    expect(api.deleteTradingViewConfig).toHaveBeenCalled();
    expect(snackBar.open).toHaveBeenCalledWith('TradingView disconnected.', 'Dismiss', {
      duration: 3000,
    });
    expect(component.tradingViewEnabled).toBe(false);

    api.deleteTradingViewConfig.mockReturnValue(throwError(() => ({ message: 'disc fail' })));
    component.disconnectTradingView();
    expect(component.tradingViewError).toBe('disc fail');
  });

  // ---------------------------------------------------------------------
  // Unsaved-changes guard
  // ---------------------------------------------------------------------

  it('hasUnsavedChanges is false with a pristine form', () => {
    fixture.detectChanges();
    expect(component.hasUnsavedChanges()).toBe(false);
  });

  it('hasUnsavedChanges is true while a save is in flight', () => {
    fixture.detectChanges();
    component.saving = true;
    expect(component.hasUnsavedChanges()).toBe(true);
  });

  it('hasUnsavedChanges is true when a secret field holds unsaved input', () => {
    fixture.detectChanges();
    component.clientSecret = 'sk-unsaved';
    expect(component.hasUnsavedChanges()).toBe(true);
    component.clientSecret = '';
    component.githubPat = 'ghp_unsaved';
    expect(component.hasUnsavedChanges()).toBe(true);
    component.githubPat = '';
    component.googleAccountPassword = 'pw';
    expect(component.hasUnsavedChanges()).toBe(true);
    component.googleAccountPassword = '';
    component.tradingViewToken = 'tv-secret';
    expect(component.hasUnsavedChanges()).toBe(true);
  });

  it('hasUnsavedChanges is true when a Slack webhook URL (a secret) is typed', () => {
    fixture.detectChanges();
    component.webhookUrl = 'https://hooks.slack.com/services/T0/B0/' + 'x'.repeat(40);
    expect(component.hasUnsavedChanges()).toBe(true);
  });
});
