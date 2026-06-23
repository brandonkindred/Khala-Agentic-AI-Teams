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
  ollama_model: '',
  claude_model: '',
  ollama_base_url: 'https://ollama.com',
  claude_api_key_configured: false,
  ollama_api_key_configured: false,
  storage_available: true,
  storage_status: 'available',
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
    // A failed load can't confirm the store is reachable, so Save is disabled and the
    // status points at connectivity (the API itself didn't answer).
    expect(component.storageAvailable).toBe(false);
    expect(component.storageStatus).toBe('unreachable');
  });

  it('reflects the unreachable storage status from the API', () => {
    apiSpy.getConfig.mockReturnValue(
      of({ ...BASE_CONFIG, storage_available: false, storage_status: 'unreachable' }),
    );
    component.loadConfig();
    expect(component.storageAvailable).toBe(false);
    expect(component.storageStatus).toBe('unreachable');
  });

  it('save guard message differs for unreachable vs unconfigured storage', () => {
    apiSpy.getConfig.mockReturnValue(
      of({ ...BASE_CONFIG, storage_available: false, storage_status: 'unreachable' }),
    );
    component.loadConfig();
    component.save();
    expect(apiSpy.updateConfig).not.toHaveBeenCalled();
    expect(component.error).toContain('unreachable');

    apiSpy.getConfig.mockReturnValue(
      of({ ...BASE_CONFIG, storage_available: false, storage_status: 'unconfigured' }),
    );
    component.loadConfig();
    component.save();
    expect(component.error).toContain('not configured');
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

  it('onProviderChange falls back to the first suggestion when no model is stored', () => {
    // BASE_CONFIG has empty per-provider models, so the switch uses the default.
    component.model = 'deepseek-v4-pro:cloud';
    component.onProviderChange('claude');
    expect(component.provider).toBe('claude');
    expect(component.model).toBe('claude-opus-4-8'); // first claude option
    component.onProviderChange('ollama');
    expect(component.model).toBe('llama3.1'); // first ollama suggestion
  });

  it('onProviderChange restores each provider\'s saved model (lossless switch)', () => {
    apiSpy.getConfig.mockReturnValue(
      of({ ...BASE_CONFIG, ollama_model: 'qwen3-coder:480b-cloud', claude_model: 'claude-sonnet-4-6' }),
    );
    component.loadConfig();
    component.onProviderChange('claude');
    expect(component.model).toBe('claude-sonnet-4-6'); // saved, not the first suggestion
    component.onProviderChange('ollama');
    expect(component.model).toBe('qwen3-coder:480b-cloud'); // saved, not the first suggestion
  });

  it('does not send a stale cross-provider model after a provider switch', () => {
    apiSpy.updateConfig.mockReturnValue(of({ ...BASE_CONFIG, provider: 'claude' }));
    // Start on Ollama with an Ollama model, then switch to Claude via the handler.
    component.model = 'deepseek-v4-pro:cloud';
    component.onProviderChange('claude');
    component.save();
    const body = apiSpy.updateConfig.mock.calls[0][0];
    expect(body.model).toBe('claude-opus-4-8'); // not the stale Ollama model
  });

  it('treats a custom host containing ollama.com as Local, not Cloud', () => {
    apiSpy.getConfig.mockReturnValue(
      of({ ...BASE_CONFIG, ollama_base_url: 'http://ollama.company.com:11434' }),
    );
    component.loadConfig();
    expect(component.ollamaMode).toBe('local');
  });

  it('defaults a cleared Local base URL to localhost instead of sending empty', () => {
    apiSpy.updateConfig.mockReturnValue(of({ ...BASE_CONFIG }));
    component.provider = 'ollama';
    component.ollamaMode = 'local';
    component.ollamaBaseUrl = '';
    component.save();
    const body = apiSpy.updateConfig.mock.calls[0][0];
    expect(body.ollama_base_url).toBe('http://localhost:11434');
  });

  it('clears a stale success banner when reloading', () => {
    component.success = 'previously saved';
    apiSpy.getConfig.mockReturnValue(of({ ...BASE_CONFIG }));
    component.loadConfig();
    expect(component.success).toBeNull();
  });

  it('blocks save with an error when the model is empty', () => {
    component.provider = 'claude';
    component.model = '   ';
    component.save();
    expect(apiSpy.updateConfig).not.toHaveBeenCalled();
    expect(component.error).toContain('model');
  });

  it('treats a scheme-less ollama.com base URL as Cloud', () => {
    apiSpy.getConfig.mockReturnValue(of({ ...BASE_CONFIG, ollama_base_url: 'ollama.com' }));
    component.loadConfig();
    expect(component.ollamaMode).toBe('cloud');
  });

  it('treats a scheme-less custom host as Local', () => {
    apiSpy.getConfig.mockReturnValue(of({ ...BASE_CONFIG, ollama_base_url: 'my-ollama.internal:11434' }));
    component.loadConfig();
    expect(component.ollamaMode).toBe('local');
  });
});
