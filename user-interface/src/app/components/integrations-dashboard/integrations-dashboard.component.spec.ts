import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { provideRouter } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
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
  };

  beforeEach(async () => {
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

    await TestBed.configureTestingModule({
      imports: [IntegrationsDashboardComponent, NoopAnimationsModule],
      providers: [provideRouter([]), { provide: IntegrationsApiService, useValue: apiSpy }],
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
    expect(component.success).toBe('Slack integration saved.');
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
