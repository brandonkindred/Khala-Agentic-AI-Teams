import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { SimpleChange } from '@angular/core';
import { vi } from 'vitest';
import { BrandingChatComponent } from './branding-chat.component';
import { BrandingApiService } from '../../services/branding-api.service';
import type { ConversationStateResponse } from '../../models';

describe('BrandingChatComponent', () => {
  let component: BrandingChatComponent;
  let fixture: ComponentFixture<BrandingChatComponent>;
  let apiSpy: {
    createConversation: ReturnType<typeof vi.fn>;
    getConversation: ReturnType<typeof vi.fn>;
    sendConversationMessage: ReturnType<typeof vi.fn>;
  };

  const mockResponse: ConversationStateResponse = {
    conversation_id: 'conv-1',
    messages: [
      { role: 'assistant', content: 'Hi! What is your company name?', timestamp: new Date().toISOString() },
    ],
    mission: { company_name: 'TBD', company_description: 'To be discussed.', target_audience: 'TBD' },
    latest_output: null,
    suggested_questions: ['What is your company name?', 'Who is your audience?'],
  } as never;

  beforeEach(async () => {
    apiSpy = {
      createConversation: vi.fn().mockReturnValue(of(mockResponse)),
      getConversation: vi.fn().mockReturnValue(of(mockResponse)),
      sendConversationMessage: vi.fn().mockReturnValue(of(mockResponse)),
    };

    await TestBed.configureTestingModule({
      imports: [BrandingChatComponent],
      providers: [{ provide: BrandingApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(BrandingChatComponent);
    component = fixture.componentInstance;
  });

  it('creates and bootstraps a new conversation', () => {
    fixture.detectChanges();
    expect(apiSpy.createConversation).toHaveBeenCalled();
    expect(component.messages.length).toBeGreaterThan(0);
  });

  it('loads existing conversation when conversationId is provided', () => {
    component.conversationId = 'conv-existing';
    fixture.detectChanges();
    expect(apiSpy.getConversation).toHaveBeenCalledWith('conv-existing');
  });

  it('handles network unreachable error on bootstrap (status 0)', () => {
    apiSpy.createConversation.mockReturnValue(throwError(() => ({ status: 0 })));
    fixture.detectChanges();
    expect(component.error).toContain("Couldn't start the conversation");
  });

  it('handles 404 unreachable error', () => {
    apiSpy.createConversation.mockReturnValue(throwError(() => ({ status: 404 })));
    fixture.detectChanges();
    expect(component.error).toContain("Couldn't start the conversation");
  });

  it('handles generic error on bootstrap', () => {
    apiSpy.createConversation.mockReturnValue(throwError(() => ({ error: { detail: 'oops' } })));
    fixture.detectChanges();
    expect(component.error).toBe('oops');
  });

  it('ngOnChanges re-bootstraps on conversationId change', () => {
    fixture.detectChanges();
    apiSpy.getConversation.mockClear();
    component.conversationId = 'conv-2';
    component.ngOnChanges({ conversationId: new SimpleChange('conv-1', 'conv-2', false) });
    expect(apiSpy.getConversation).toHaveBeenCalled();
  });

  it('onSubmit skipped when invalid or loading', () => {
    fixture.detectChanges();
    component.form.setValue({ message: '' });
    component.onSubmit();
    expect(apiSpy.sendConversationMessage).not.toHaveBeenCalled();
    component.form.setValue({ message: 'hello' });
    component.loading = true;
    component.onSubmit();
    expect(apiSpy.sendConversationMessage).not.toHaveBeenCalled();
  });

  it('onSubmit sends via existing conversation', () => {
    fixture.detectChanges();
    component.form.setValue({ message: 'hello' });
    component.onSubmit();
    expect(apiSpy.sendConversationMessage).toHaveBeenCalledWith('conv-1', 'hello', false);
  });

  it('onSubmit creates new conversation if not present', () => {
    fixture.detectChanges();
    (component as unknown as { _conversationId: string | null })._conversationId = null;
    component.form.setValue({ message: 'hello' });
    apiSpy.createConversation.mockClear();
    component.onSubmit();
    expect(apiSpy.createConversation).toHaveBeenCalledWith('hello', false);
  });

  it('onSubmit sendConversationMessage error', () => {
    fixture.detectChanges();
    apiSpy.sendConversationMessage.mockReturnValue(
      throwError(() => ({ error: { detail: 'oops' } })),
    );
    component.form.setValue({ message: 'hello' });
    component.onSubmit();
    expect(component.error).toBe('oops');
  });

  it('onSubmit createConversation(message) error path', () => {
    fixture.detectChanges();
    (component as unknown as { _conversationId: string | null })._conversationId = null;
    apiSpy.createConversation.mockReturnValue(throwError(() => ({ error: { detail: 'oops' } })));
    component.form.setValue({ message: 'hello' });
    component.onSubmit();
    expect(component.error).toBe('oops');
  });

  it('onSuggestedQuestion calls sendMessage', () => {
    fixture.detectChanges();
    component.onSuggestedQuestion('Whats your name?');
    expect(apiSpy.sendConversationMessage).toHaveBeenCalled();
  });

  it('retryStartConversation calls createConversation', () => {
    fixture.detectChanges();
    apiSpy.createConversation.mockClear();
    component.retryStartConversation();
    expect(apiSpy.createConversation).toHaveBeenCalled();
  });

  it('retryStartConversation handles unreachable error', () => {
    fixture.detectChanges();
    apiSpy.createConversation.mockReturnValue(throwError(() => ({ status: 0 })));
    component.retryStartConversation();
    expect(component.error).toContain("Couldn't start the conversation");
  });

  it('formatTime returns formatted or empty', () => {
    fixture.detectChanges();
    expect(component.formatTime('')).toBe('');
    expect(component.formatTime('2025-01-01T12:00:00Z').length).toBeGreaterThan(0);
  });

  it('emits brandAutoCreated when brand_id appears', () => {
    const spy = vi.fn();
    component.brandAutoCreated.subscribe(spy);
    apiSpy.createConversation.mockReturnValue(of({ ...mockResponse, brand_id: 'b1' } as never));
    fixture.detectChanges();
    expect(spy).toHaveBeenCalledWith('b1');
  });
});
