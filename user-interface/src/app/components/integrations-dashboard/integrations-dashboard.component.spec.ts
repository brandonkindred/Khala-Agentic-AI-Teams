import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { provideRouter } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatSnackBar } from '@angular/material/snack-bar';
import { vi } from 'vitest';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { IntegrationsDashboardComponent } from './integrations-dashboard.component';

describe('IntegrationsDashboardComponent', () => {
  let component: IntegrationsDashboardComponent;
  let fixture: ComponentFixture<IntegrationsDashboardComponent>;
  let apiSpy: {
    getSlackConfig: ReturnType<typeof vi.fn>;
    updateSlackConfig: ReturnType<typeof vi.fn>;
    getGoogleBrowserLoginStatus: ReturnType<typeof vi.fn>;
    getMediumConfig: ReturnType<typeof vi.fn>;
    updateMediumConfig: ReturnType<typeof vi.fn>;
    mediumBrowserLoginSession: ReturnType<typeof vi.fn>;
    getSlackOAuthUrl: ReturnType<typeof vi.fn>;
    disconnectSlack: ReturnType<typeof vi.fn>;
    putGoogleBrowserLoginCredentials: ReturnType<typeof vi.fn>;
    deleteGoogleBrowserLoginCredentials: ReturnType<typeof vi.fn>;
    getGitHubConfig: ReturnType<typeof vi.fn>;
    updateGitHubConfig: ReturnType<typeof vi.fn>;
    deleteGitHubConfig: ReturnType<typeof vi.fn>;
  };
  let snackBar: { open: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    snackBar = { open: vi.fn() };
    apiSpy = {
      getSlackConfig: vi.fn(),
      updateSlackConfig: vi.fn(),
      getGoogleBrowserLoginStatus: vi.fn(),
      getMediumConfig: vi.fn(),
      updateMediumConfig: vi.fn(),
      mediumBrowserLoginSession: vi.fn(),
      getSlackOAuthUrl: vi.fn(),
      disconnectSlack: vi.fn(),
      putGoogleBrowserLoginCredentials: vi.fn(),
      deleteGoogleBrowserLoginCredentials: vi.fn(),
      getGitHubConfig: vi.fn(),
      updateGitHubConfig: vi.fn(),
      deleteGitHubConfig: vi.fn(),
    };
    apiSpy.getSlackConfig.mockReturnValue(of({
      enabled: false,
      webhook_configured: false,
      bot_token_configured: false,
      channel_display_name: '',
      default_channel: '',
      notify_open_questions: true,
      notify_pa_responses: true,
    }));
    apiSpy.getGoogleBrowserLoginStatus.mockReturnValue(of({ configured: false }));
    apiSpy.getMediumConfig.mockReturnValue(of({ enabled: false }));
    apiSpy.getGitHubConfig.mockReturnValue(of({ enabled: false, token_configured: false, owner: '', repo: '', default_label: '' }));

    await TestBed.configureTestingModule({
      imports: [IntegrationsDashboardComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: IntegrationsApiService, useValue: apiSpy },
        { provide: MatSnackBar, useValue: snackBar },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(IntegrationsDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should load Slack config on init', () => {
    expect(apiSpy.getSlackConfig).toHaveBeenCalled();
    expect(component.slackEnabled).toBe(false);
    expect(component.loadingSlack).toBe(false);
  });

  it('should set error when loadSlackConfig fails', () => {
    apiSpy.getSlackConfig.mockReturnValue(throwError(() => ({ error: { detail: 'Network error' } })));
    component.loadSlackConfig();
    expect(component.error).toBeTruthy();
    expect(component.loadingSlack).toBe(false);
  });

  it('flags the GitHub credential store as unreachable from the API', () => {
    apiSpy.getGitHubConfig.mockReturnValue(
      of({ enabled: true, token_configured: false, owner: 'acme', repo: 'widget', default_label: '', credential_store_unreachable: true }),
    );
    component.loadGitHubConfig();
    expect(component.githubStoreUnreachable).toBe(true);
  });

  it('reflects the webhook-secret configured flag from the API on load', () => {
    apiSpy.getGitHubConfig.mockReturnValue(
      of({ enabled: true, token_configured: true, owner: 'acme', repo: 'widget', default_label: '', webhook_secret_configured: true }),
    );
    component.githubWebhookSecret = 'leftover';
    component.loadGitHubConfig();
    expect(component.githubWebhookSecretConfigured).toBe(true);
    // The write-only input is always cleared after a load so a saved secret is never re-sent.
    expect(component.githubWebhookSecret).toBe('');
  });

  it('sends the webhook secret on save and clears the input afterwards', () => {
    apiSpy.updateGitHubConfig.mockReturnValue(
      of({ enabled: true, token_configured: true, owner: 'acme', repo: 'widget', default_label: '', webhook_secret_configured: true }),
    );
    component.githubEnabled = true;
    component.githubOwner = 'acme';
    component.githubRepo = 'widget';
    component.githubWebhookSecret = 'whsec_abc';
    component.saveGitHubConfig();
    expect(apiSpy.updateGitHubConfig).toHaveBeenCalledWith(
      expect.objectContaining({ webhook_secret: 'whsec_abc' }),
    );
    expect(component.githubWebhookSecretConfigured).toBe(true);
    expect(component.githubWebhookSecret).toBe('');
  });

  it('defaults the GitHub store-unreachable flag to false when absent', () => {
    apiSpy.getGitHubConfig.mockReturnValue(
      of({ enabled: false, token_configured: false, owner: '', repo: '', default_label: '' }),
    );
    component.loadGitHubConfig();
    expect(component.githubStoreUnreachable).toBe(false);
  });

  it('clears a stale GitHub store-unreachable flag when a reload fails', () => {
    apiSpy.getGitHubConfig.mockReturnValue(
      of({ enabled: true, token_configured: false, owner: 'acme', repo: 'widget', default_label: '', credential_store_unreachable: true }),
    );
    component.loadGitHubConfig();
    expect(component.githubStoreUnreachable).toBe(true);
    // A later reload errors out: current state is unknown, so the stale banner flag
    // must be cleared rather than left visible alongside the error.
    apiSpy.getGitHubConfig.mockReturnValue(throwError(() => ({ error: { detail: 'blip' } })));
    component.loadGitHubConfig();
    expect(component.githubStoreUnreachable).toBe(false);
    expect(component.githubError).toBe('blip');
  });

  it('webhookUrlInvalid returns true for short or invalid URL', () => {
    component.webhookUrl = 'https://hooks.slack.com/x';
    expect(component.webhookUrlInvalid()).toBe(true);
    component.webhookUrl = 'https://other.com/x';
    expect(component.webhookUrlInvalid()).toBe(true);
  });

  it('webhookUrlInvalid returns false when empty', () => {
    component.webhookUrl = '';
    expect(component.webhookUrlInvalid()).toBe(false);
  });

  it('should call updateSlackConfig and set success on save', () => {
    component.slackEnabled = true;
    component.webhookUrl = 'https://hooks.slack.com/services/T00/B00/xxxxxxxxxxxxxxxxxxxxxxxx';
    component.channelDisplayName = '#eng';
    apiSpy.updateSlackConfig.mockReturnValue(of({
      enabled: true,
      webhook_configured: true,
      channel_display_name: '#eng',
      default_channel: '',
      notify_open_questions: true,
      notify_pa_responses: true,
    }));
    component.saveAdvanced();
    expect(apiSpy.updateSlackConfig).toHaveBeenCalledWith(expect.objectContaining({
      enabled: true,
      channel_display_name: '#eng',
    }));
    expect(snackBar.open).toHaveBeenCalledWith('Slack integration saved.', 'Dismiss', { duration: 3000 });
    expect(component.saving).toBe(false);
  });

  it('should set error when save fails', () => {
    apiSpy.updateSlackConfig.mockReturnValue(throwError(() => ({ error: { detail: 'Save failed' } })));
    component.saveAdvanced();
    expect(component.error).toBeTruthy();
    expect(component.saving).toBe(false);
  });

  it('should set client error when webhook required but invalid', () => {
    component.slackEnabled = true;
    component.webhookConfigured = false;
    component.webhookUrl = 'bad';
    component.saveAdvanced();
    expect(component.error).toContain('Webhook URL');
    expect(apiSpy.updateSlackConfig).not.toHaveBeenCalled();
  });
});
