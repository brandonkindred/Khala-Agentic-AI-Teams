import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { InvestmentApiService } from '../../services/investment-api.service';
import { InvestmentChatComponent } from './investment-chat.component';

describe('InvestmentChatComponent', () => {
  let component: InvestmentChatComponent;
  let fixture: ComponentFixture<InvestmentChatComponent>;
  let apiSpy: {
    startAdvisorSession: ReturnType<typeof vi.fn>;
    sendAdvisorMessage: ReturnType<typeof vi.fn>;
    completeAdvisorSession: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    apiSpy = {
      startAdvisorSession: vi
        .fn()
        .mockReturnValue(of({ session_id: 's1', session_status: 'active' })),
      sendAdvisorMessage: vi
        .fn()
        .mockReturnValue(
          of({ session_status: 'active', advisor_message: 'hi', current_topic: 'profile', missing_fields: [] }),
        ),
      completeAdvisorSession: vi.fn().mockReturnValue(of({ message: 'done', ips: { profile: {} } })),
    };
    await TestBed.configureTestingModule({
      imports: [InvestmentChatComponent, NoopAnimationsModule],
      providers: [{ provide: InvestmentApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(InvestmentChatComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('initialises with a greeting', () => {
    expect(component.messages.length).toBe(1);
    expect(component.messages[0].role).toBe('assistant');
  });

  it('onSubmit does nothing if invalid', () => {
    component.form.setValue({ message: '' });
    component.onSubmit();
    expect(apiSpy.startAdvisorSession).not.toHaveBeenCalled();
  });

  it('onSubmit does nothing while loading', () => {
    component.form.setValue({ message: 'hi' });
    component.loading = true;
    component.onSubmit();
    expect(apiSpy.startAdvisorSession).not.toHaveBeenCalled();
  });

  it('onSubmit creates session on first send', () => {
    component.form.setValue({ message: 'hello' });
    component.onSubmit();
    expect(apiSpy.startAdvisorSession).toHaveBeenCalledWith({ user_id: 'default' });
    expect(apiSpy.sendAdvisorMessage).toHaveBeenCalledWith('s1', { message: 'hello' });
    expect(component.sessionId).toBe('s1');
    expect(component.loading).toBe(false);
  });

  it('onSubmit on existing session uses sendAdvisorMessage', () => {
    component.sessionId = 's2';
    component.form.setValue({ message: 'second message' });
    component.onSubmit();
    expect(apiSpy.startAdvisorSession).not.toHaveBeenCalled();
    expect(apiSpy.sendAdvisorMessage).toHaveBeenCalledWith('s2', { message: 'second message' });
  });

  it('onSubmit handles error during session start', () => {
    apiSpy.startAdvisorSession.mockReturnValue(throwError(() => ({ error: { detail: 'boom' } })));
    component.form.setValue({ message: 'hi' });
    component.onSubmit();
    expect(component.loading).toBe(false);
    const last = component.messages[component.messages.length - 1];
    expect(last.content).toContain('boom');
  });

  it('onSubmit handles error from sendAdvisorMessage', () => {
    apiSpy.sendAdvisorMessage.mockReturnValue(throwError(() => ({ message: 'late boom' })));
    component.sessionId = 's2';
    component.form.setValue({ message: 'second message' });
    component.onSubmit();
    expect(component.loading).toBe(false);
    const last = component.messages[component.messages.length - 1];
    expect(last.content).toContain('late boom');
  });

  it('onQuickAction sends predefined message', () => {
    component.onQuickAction({ label: 'X', message: 'Hello there' });
    expect(apiSpy.startAdvisorSession).toHaveBeenCalled();
  });

  it('onConfirmProfile no-ops without session', () => {
    component.onConfirmProfile();
    expect(apiSpy.completeAdvisorSession).not.toHaveBeenCalled();
  });

  it('onConfirmProfile success emits profileCreated', () => {
    component.sessionId = 's3';
    const spy = vi.fn();
    component.profileCreated.subscribe(spy);
    component.onConfirmProfile();
    expect(apiSpy.completeAdvisorSession).toHaveBeenCalledWith('s3');
    expect(spy).toHaveBeenCalled();
    expect(component.sessionStatus).toBe('completed');
    expect(component.loading).toBe(false);
  });

  it('onConfirmProfile success without IPS still completes', () => {
    apiSpy.completeAdvisorSession.mockReturnValue(of({ message: 'done' }));
    component.sessionId = 's3';
    const spy = vi.fn();
    component.profileCreated.subscribe(spy);
    component.onConfirmProfile();
    expect(spy).not.toHaveBeenCalled();
    expect(component.sessionStatus).toBe('completed');
  });

  it('onConfirmProfile error path', () => {
    apiSpy.completeAdvisorSession.mockReturnValue(throwError(() => ({ error: { detail: 'oops' } })));
    component.sessionId = 's3';
    component.onConfirmProfile();
    const last = component.messages[component.messages.length - 1];
    expect(last.content).toContain('oops');
    expect(component.loading).toBe(false);
  });

  it('formatTime returns string', () => {
    expect(component.formatTime('2025-01-01T12:00:00Z').length).toBeGreaterThan(0);
  });
});
