import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { SimpleChange } from '@angular/core';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { TeamAssistantApiService } from '../../services/team-assistant-api.service';
import { TeamAssistantChatComponent } from './team-assistant-chat.component';

describe('TeamAssistantChatComponent', () => {
  let component: TeamAssistantChatComponent;
  let fixture: ComponentFixture<TeamAssistantChatComponent>;
  let apiSpy: {
    getConversation: ReturnType<typeof vi.fn>;
    sendMessage: ReturnType<typeof vi.fn>;
    updateContext: ReturnType<typeof vi.fn>;
    getReadiness: ReturnType<typeof vi.fn>;
    launch: ReturnType<typeof vi.fn>;
    resetConversation: ReturnType<typeof vi.fn>;
  };

  const stateResponse = {
    conversation_id: 'c1',
    messages: [{ role: 'assistant', content: 'hi', timestamp: '2025-01-01T00:00:00Z' }],
    context: { foo: 'bar' },
    suggested_questions: ['Q1?'],
  };

  beforeEach(async () => {
    apiSpy = {
      getConversation: vi.fn().mockReturnValue(of(stateResponse)),
      sendMessage: vi.fn().mockReturnValue(of(stateResponse)),
      updateContext: vi.fn().mockReturnValue(of(stateResponse)),
      getReadiness: vi.fn().mockReturnValue(of({ ready: true, missing_fields: [] })),
      launch: vi.fn().mockReturnValue(
        of({ job_id: 'j1', conversation_id: 'c1', upstream_status: 200, upstream_body: { ok: true } }),
      ),
      resetConversation: vi.fn().mockReturnValue(of(stateResponse)),
    };
    await TestBed.configureTestingModule({
      imports: [TeamAssistantChatComponent, NoopAnimationsModule],
      providers: [{ provide: TeamAssistantApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(TeamAssistantChatComponent);
    component = fixture.componentInstance;
    component.teamApiUrl = '/api/x/assistant';
  });

  it('creates and loads conversation on init', () => {
    fixture.detectChanges();
    expect(apiSpy.getConversation).toHaveBeenCalled();
    expect(component.messages.length).toBe(1);
    expect(component.context).toEqual({ foo: 'bar' });
  });

  it('ngOnChanges reloads when conversationId changes', () => {
    fixture.detectChanges();
    apiSpy.getConversation.mockClear();
    component.ngOnChanges({ conversationId: new SimpleChange('c1', 'c2', false) });
    expect(apiSpy.getConversation).toHaveBeenCalled();
  });

  it('ngOnChanges first change ignored', () => {
    fixture.detectChanges();
    apiSpy.getConversation.mockClear();
    component.ngOnChanges({ conversationId: new SimpleChange(undefined, 'c1', true) });
    expect(apiSpy.getConversation).not.toHaveBeenCalled();
  });

  it('loadConversation early-exits without teamApiUrl', () => {
    component.teamApiUrl = '';
    apiSpy.getConversation.mockClear();
    fixture.detectChanges();
    expect(apiSpy.getConversation).not.toHaveBeenCalled();
  });

  it('loadConversation error sets error', () => {
    apiSpy.getConversation.mockReturnValue(throwError(() => ({ error: { detail: 'boom' } })));
    fixture.detectChanges();
    expect(component.error).toBe('boom');
    expect(component.loading).toBe(false);
  });

  it('onSubmit does nothing when invalid', () => {
    fixture.detectChanges();
    component.chatForm.setValue({ message: '' });
    apiSpy.sendMessage.mockClear();
    component.onSubmit();
    expect(apiSpy.sendMessage).not.toHaveBeenCalled();
  });

  it('onSubmit does nothing while loading', () => {
    fixture.detectChanges();
    component.chatForm.setValue({ message: 'hi' });
    component.loading = true;
    apiSpy.sendMessage.mockClear();
    component.onSubmit();
    expect(apiSpy.sendMessage).not.toHaveBeenCalled();
  });

  it('onSubmit sends a message and applies state', () => {
    fixture.detectChanges();
    component.chatForm.setValue({ message: 'hello' });
    component.onSubmit();
    expect(apiSpy.sendMessage).toHaveBeenCalledWith('/api/x/assistant', 'hello', undefined);
  });

  it('onSubmit error sets error', () => {
    fixture.detectChanges();
    apiSpy.sendMessage.mockReturnValue(throwError(() => ({ error: { detail: 'oops' } })));
    component.chatForm.setValue({ message: 'hello' });
    component.onSubmit();
    expect(component.error).toBe('oops');
  });

  it('onSuggestedQuestion calls sendMessage', () => {
    fixture.detectChanges();
    component.onSuggestedQuestion('Question?');
    expect(apiSpy.sendMessage).toHaveBeenCalledWith('/api/x/assistant', 'Question?', undefined);
  });

  it('onLaunch emits workflowLaunched on success', () => {
    fixture.detectChanges();
    const spy = vi.fn();
    component.workflowLaunched.subscribe(spy);
    component.onLaunch();
    expect(spy).toHaveBeenCalledWith({
      job_id: 'j1',
      conversation_id: 'c1',
      upstream_status: 200,
      upstream_body: { ok: true },
    });
  });

  it('onLaunch handles missing_required_fields error', () => {
    fixture.detectChanges();
    apiSpy.launch.mockReturnValue(
      throwError(() => ({ error: { detail: { error: 'missing_required_fields', missing: ['x'] } } })),
    );
    component.onLaunch();
    expect(component.error).toContain('Still missing: x');
  });

  it('onLaunch handles generic object detail', () => {
    fixture.detectChanges();
    apiSpy.launch.mockReturnValue(
      throwError(() => ({ error: { detail: { message: 'specific' } } })),
    );
    component.onLaunch();
    expect(component.error).toBe('specific');
  });

  it('onLaunch handles string detail', () => {
    fixture.detectChanges();
    apiSpy.launch.mockReturnValue(throwError(() => ({ error: { detail: 'boom' } })));
    component.onLaunch();
    expect(component.error).toBe('boom');
  });

  it('onLaunch no-op without teamApiUrl', () => {
    component.teamApiUrl = '';
    apiSpy.launch.mockClear();
    component.onLaunch();
    expect(apiSpy.launch).not.toHaveBeenCalled();
  });

  it('retryLoad resets error and reloads', () => {
    fixture.detectChanges();
    component.error = 'old error';
    apiSpy.getConversation.mockClear();
    component.retryLoad();
    expect(component.error).toBeNull();
    expect(apiSpy.getConversation).toHaveBeenCalled();
  });

  it('resetConversation success applies state', () => {
    fixture.detectChanges();
    component.resetConversation();
    expect(apiSpy.resetConversation).toHaveBeenCalled();
    expect(component.loading).toBe(false);
  });

  it('resetConversation error sets error', () => {
    fixture.detectChanges();
    apiSpy.resetConversation.mockReturnValue(throwError(() => ({ error: { detail: 'oops' } })));
    component.resetConversation();
    expect(component.error).toBe('oops');
  });

  it('resetConversation no-op without teamApiUrl', () => {
    component.teamApiUrl = '';
    apiSpy.resetConversation.mockClear();
    component.resetConversation();
    expect(apiSpy.resetConversation).not.toHaveBeenCalled();
  });

  it('startEdit + cancelEdit + saveEdit', () => {
    fixture.detectChanges();
    component.context = { foo: 'old' };
    component.startEdit('foo');
    expect(component.editingField).toBe('foo');
    expect(component.editingValue).toBe('old');
    component.cancelEdit();
    expect(component.editingField).toBeNull();

    component.startEdit('foo');
    component.editingValue = 'new';
    component.saveEdit();
    expect(apiSpy.updateContext).toHaveBeenCalledWith(
      '/api/x/assistant',
      { foo: 'new' },
      undefined,
    );
    expect(component.editingField).toBeNull();
  });

  it('saveEdit no-op without editing field', () => {
    fixture.detectChanges();
    apiSpy.updateContext.mockClear();
    component.saveEdit();
    expect(apiSpy.updateContext).not.toHaveBeenCalled();
  });

  it('startEdit handles undefined context value', () => {
    fixture.detectChanges();
    component.context = {};
    component.startEdit('missing');
    expect(component.editingValue).toBe('');
  });

  it('fieldValue/isFieldFilled', () => {
    fixture.detectChanges();
    component.context = { a: 'x', b: null };
    expect(component.fieldValue('a')).toBe('x');
    expect(component.fieldValue('b')).toBe('');
    expect(component.isFieldFilled('a')).toBe(true);
    expect(component.isFieldFilled('b')).toBe(false);
  });

  it('formatTime returns formatted or empty', () => {
    fixture.detectChanges();
    expect(component.formatTime('')).toBe('');
    expect(component.formatTime('2025-01-01T12:34:00Z').length).toBeGreaterThan(0);
  });

  it('emits conversationLoaded when state has conversation_id', () => {
    const spy = vi.fn();
    component.conversationLoaded.subscribe(spy);
    fixture.detectChanges();
    expect(spy).toHaveBeenCalledWith('c1');
  });
});
