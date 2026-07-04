import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { TeamAssistantApiService } from '../../services/team-assistant-api.service';
import { TeamAssistantChatComponent } from './team-assistant-chat.component';
import { expectNoAxeViolations } from '../../testing/a11y';

describe('TeamAssistantChatComponent a11y', () => {
  const stateResponse = {
    conversation_id: 'c1',
    messages: [{ role: 'assistant', content: 'hi', timestamp: '2025-01-01T00:00:00Z' }],
    context: { foo: 'bar' },
    suggested_questions: ['Q1?'],
  };

  const setup = async (messages: (typeof stateResponse)['messages']) => {
    const state = { ...stateResponse, messages };
    const api = {
      getConversation: vi.fn().mockReturnValue(of(state)),
      sendMessage: vi.fn().mockReturnValue(of(state)),
      updateContext: vi.fn().mockReturnValue(of(state)),
      getReadiness: vi.fn().mockReturnValue(of({ ready: true, missing_fields: [] })),
      launch: vi
        .fn()
        .mockReturnValue(of({ job_id: 'j1', conversation_id: 'c1', upstream_status: 200, upstream_body: { ok: true } })),
      resetConversation: vi.fn().mockReturnValue(of(state)),
    };
    await TestBed.configureTestingModule({
      imports: [TeamAssistantChatComponent, NoopAnimationsModule],
      providers: [{ provide: TeamAssistantApiService, useValue: api }],
    }).compileComponents();
  };

  it('has no axe violations with a loaded conversation', async () => {
    await setup(stateResponse.messages);

    const fixture = TestBed.createComponent(TeamAssistantChatComponent);
    // Without teamApiUrl, ngOnInit early-returns and nothing loads.
    fixture.componentInstance.teamApiUrl = '/api/x/assistant';
    fixture.detectChanges();

    // Guard: the two-panel chat layout + an assistant message rendered.
    expect(fixture.nativeElement.querySelector('.chat-layout')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.message.assistant')).toBeTruthy();

    await expectNoAxeViolations(fixture.nativeElement);
  });

  it('has no axe violations with an empty conversation', async () => {
    await setup([]);

    const fixture = TestBed.createComponent(TeamAssistantChatComponent);
    fixture.componentInstance.teamApiUrl = '/api/x/assistant';
    fixture.detectChanges();

    // Guard: the layout still renders with no messages, so axe isn't vacuous.
    expect(fixture.nativeElement.querySelector('.chat-layout')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.message')).toBeFalsy();

    await expectNoAxeViolations(fixture.nativeElement);
  });
});
