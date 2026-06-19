import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { LlmConfigApiService } from '../../services/llm-config-api.service';
import { LlmConfigDashboardComponent } from './llm-config-dashboard.component';
import type { LlmConfigResponse } from '../../models/llm-config.model';

const BASE_CONFIG: LlmConfigResponse = {
  provider: 'ollama',
  model: 'deepseek-v4-pro:cloud',
  ollama_base_url: 'https://ollama.com',
  claude_api_key_configured: false,
  ollama_api_key_configured: false,
  storage_available: true,
  provider_options: ['ollama', 'claude'],
  claude_model_options: ['claude-opus-4-8'],
  ollama_model_suggestions: ['llama3.1'],
};

describe('LlmConfigDashboardComponent', () => {
  let component: LlmConfigDashboardComponent;
  let fixture: ComponentFixture<LlmConfigDashboardComponent>;
  let apiSpy: {
    getConfig: ReturnType<typeof vi.fn>;
    updateConfig: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    apiSpy = { getConfig: vi.fn(), updateConfig: vi.fn() };
    apiSpy.getConfig.mockReturnValue(of({ ...BASE_CONFIG }));

    await TestBed.configureTestingModule({
      imports: [LlmConfigDashboardComponent, NoopAnimationsModule],
      providers: [{ provide: LlmConfigApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(LlmConfigDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('should create and load config on init', () => {
    expect(component).toBeTruthy();
    expect(apiSpy.getConfig).toHaveBeenCalled();
    expect(component.provider).toBe('ollama');
    expect(component.loading).toBe(false);
  });

  it('derives Ollama cloud mode from base URL', () => {
    expect(component.ollamaMode).toBe('cloud');
  });

  it('derives Ollama local mode from a non-cloud base URL', () => {
    apiSpy.getConfig.mockReturnValue(
      of({ ...BASE_CONFIG, ollama_base_url: 'http://localhost:11434' }),
    );
    component.loadConfig();
    expect(component.ollamaMode).toBe('local');
  });

  it('sets error when load fails', () => {
    apiSpy.getConfig.mockReturnValue(throwError(() => ({ error: { detail: 'boom' } })));
    component.loadConfig();
    expect(component.error).toBe('boom');
    expect(component.loading).toBe(false);
  });

  it('onOllamaModeChange defaults the base URL', () => {
    component.onOllamaModeChange('local');
    expect(component.ollamaBaseUrl).toContain('localhost');
    component.onOllamaModeChange('cloud');
    expect(component.ollamaBaseUrl).toContain('ollama.com');
  });

  it('modelOptions follow the selected provider', () => {
    component.provider = 'claude';
    expect(component.modelOptions).toEqual(['claude-opus-4-8']);
    component.provider = 'ollama';
    expect(component.modelOptions).toEqual(['llama3.1']);
  });

  it('saves claude config with the api key when provided', () => {
    apiSpy.updateConfig.mockReturnValue(of({ ...BASE_CONFIG, provider: 'claude' }));
    component.provider = 'claude';
    component.model = 'claude-opus-4-8';
    component.claudeApiKey = 'sk-secret';
    component.save();
    expect(apiSpy.updateConfig).toHaveBeenCalledWith(
      expect.objectContaining({ provider: 'claude', model: 'claude-opus-4-8', claude_api_key: 'sk-secret' }),
    );
    expect(component.success).toBeTruthy();
    expect(component.saving).toBe(false);
  });

  it('omits a blank api key on save (preserve existing)', () => {
    apiSpy.updateConfig.mockReturnValue(of({ ...BASE_CONFIG, provider: 'claude' }));
    component.provider = 'claude';
    component.claudeApiKey = '';
    component.save();
    const body = apiSpy.updateConfig.mock.calls[0][0];
    expect(body.claude_api_key).toBeUndefined();
  });

  it('sends ollama base URL only for ollama provider', () => {
    apiSpy.updateConfig.mockReturnValue(of({ ...BASE_CONFIG }));
    component.provider = 'ollama';
    component.onOllamaModeChange('local');
    component.ollamaBaseUrl = 'http://localhost:11434';
    component.save();
    const body = apiSpy.updateConfig.mock.calls[0][0];
    expect(body.ollama_base_url).toBe('http://localhost:11434');
  });

  it('blocks save and reports when storage is unavailable', () => {
    apiSpy.getConfig.mockReturnValue(of({ ...BASE_CONFIG, storage_available: false }));
    component.loadConfig();
    component.save();
    expect(apiSpy.updateConfig).not.toHaveBeenCalled();
    expect(component.error).toContain('storage');
  });

  it('sets error when save fails', () => {
    apiSpy.updateConfig.mockReturnValue(throwError(() => ({ error: { detail: 'save failed' } })));
    component.save();
    expect(component.error).toBe('save failed');
    expect(component.saving).toBe(false);
  });
});
