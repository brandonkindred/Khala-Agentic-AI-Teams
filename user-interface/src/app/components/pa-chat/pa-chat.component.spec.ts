import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import { PaChatComponent } from './pa-chat.component';

describe('PaChatComponent', () => {
  let component: PaChatComponent;
  let fixture: ComponentFixture<PaChatComponent>;
  let apiSpy: { sendMessage: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    apiSpy = {
      sendMessage: vi.fn().mockReturnValue(of({ response: 'hi there', timestamp: '2025-01-01T00:00:00Z' })),
    };
    await TestBed.configureTestingModule({
      imports: [PaChatComponent, NoopAnimationsModule],
      providers: [{ provide: PersonalAssistantApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(PaChatComponent);
    component = fixture.componentInstance;
    component.userId = 'u1';
    fixture.detectChanges();
  });

  it('creates and adds greeting on init', () => {
    expect(component).toBeTruthy();
    expect(component.messages.length).toBeGreaterThan(0);
    expect(component.messages[0].role).toBe('assistant');
  });

  it('onSubmit does nothing when invalid', () => {
    component.form.setValue({ message: '' });
    component.onSubmit();
    expect(apiSpy.sendMessage).not.toHaveBeenCalled();
  });

  it('onSubmit does nothing while loading', () => {
    component.form.setValue({ message: 'hi' });
    component.loading = true;
    component.onSubmit();
    expect(apiSpy.sendMessage).not.toHaveBeenCalled();
  });

  it('onSubmit posts and appends both user and assistant messages', () => {
    component.form.setValue({ message: 'hi there' });
    const before = component.messages.length;
    component.onSubmit();
    expect(apiSpy.sendMessage).toHaveBeenCalledWith('u1', { message: 'hi there' });
    expect(component.messages.length).toBe(before + 2);
    expect(component.loading).toBe(false);
  });

  it('onSubmit assistant response falls back to message', () => {
    apiSpy.sendMessage.mockReturnValue(of({ message: 'fallback' }));
    component.form.setValue({ message: 'hi' });
    component.onSubmit();
    const last = component.messages[component.messages.length - 1];
    expect(last.content).toBe('fallback');
  });

  it('onSubmit assistant response defaults when both empty', () => {
    apiSpy.sendMessage.mockReturnValue(of({}));
    component.form.setValue({ message: 'hi' });
    component.onSubmit();
    const last = component.messages[component.messages.length - 1];
    expect(last.content).toBe('I processed your request.');
  });

  it('onSubmit error appends assistant error message', () => {
    apiSpy.sendMessage.mockReturnValue(throwError(() => ({ error: { detail: 'boom' } })));
    component.form.setValue({ message: 'hi' });
    component.onSubmit();
    const last = component.messages[component.messages.length - 1];
    expect(last.content).toContain('boom');
    expect(component.loading).toBe(false);
  });

  it('onQuickAction sends the predefined message', () => {
    component.onQuickAction({ label: 'X', message: "What's on my calendar?" });
    expect(apiSpy.sendMessage).toHaveBeenCalledWith('u1', { message: "What's on my calendar?" });
  });

  it('formatTime returns time string', () => {
    expect(component.formatTime('2025-01-01T12:34:00Z').length).toBeGreaterThan(0);
  });
});
