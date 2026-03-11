import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { BrandingChatComponent } from './branding-chat.component';
import { BrandingApiService } from '../../services/branding-api.service';
import type { ConversationStateResponse } from '../../models';

describe('BrandingChatComponent', () => {
  let component: BrandingChatComponent;
  let fixture: ComponentFixture<BrandingChatComponent>;
  let apiSpy: jasmine.SpyObj<BrandingApiService>;

  const mockResponse: ConversationStateResponse = {
    conversation_id: 'conv-1',
    messages: [
      { role: 'assistant', content: 'Hi! What is your company name?', timestamp: new Date().toISOString() },
    ],
    mission: {
      company_name: 'TBD',
      company_description: 'To be discussed.',
      target_audience: 'TBD',
    },
    latest_output: null,
    suggested_questions: ['What is your company name?', 'Who is your audience?'],
  };

  beforeEach(async () => {
    apiSpy = jasmine.createSpyObj('BrandingApiService', ['createConversation', 'getConversation', 'sendConversationMessage']);
    apiSpy.createConversation.and.returnValue(of(mockResponse));
    apiSpy.getConversation.and.returnValue(of(mockResponse));
    apiSpy.sendConversationMessage.and.returnValue(of(mockResponse));

    await TestBed.configureTestingModule({
      imports: [BrandingChatComponent],
      providers: [{ provide: BrandingApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(BrandingChatComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should call createConversation on init when no conversationId', () => {
    expect(apiSpy.createConversation).toHaveBeenCalled();
  });

  it('should display initial assistant message', () => {
    expect(component.messages.length).toBeGreaterThanOrEqual(1);
    expect(component.messages[0].role).toBe('assistant');
  });
});
