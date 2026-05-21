import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import { PaCalendarComponent } from './pa-calendar.component';

describe('PaCalendarComponent', () => {
  let component: PaCalendarComponent;
  let fixture: ComponentFixture<PaCalendarComponent>;
  let apiSpy: { createEventFromText: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    apiSpy = { createEventFromText: vi.fn().mockReturnValue(of({ success: true, created_event_ids: ['e1'] })) };
    await TestBed.configureTestingModule({
      imports: [PaCalendarComponent, NoopAnimationsModule],
      providers: [{ provide: PersonalAssistantApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(PaCalendarComponent);
    component = fixture.componentInstance;
    component.userId = 'u1';
    fixture.detectChanges();
  });

  it('creates', () => {
    expect(component).toBeTruthy();
  });

  it('onCreateEvent does nothing if form invalid', () => {
    component.form.setValue({ eventText: 'ab' });
    component.onCreateEvent();
    expect(apiSpy.createEventFromText).not.toHaveBeenCalled();
  });

  it('onCreateEvent does nothing while loading', () => {
    component.form.setValue({ eventText: 'Lunch with team' });
    component.loading = true;
    component.onCreateEvent();
    expect(apiSpy.createEventFromText).not.toHaveBeenCalled();
  });

  it('onCreateEvent success creates event', () => {
    component.form.setValue({ eventText: 'Lunch with team' });
    component.onCreateEvent();
    expect(apiSpy.createEventFromText).toHaveBeenCalledWith('u1', { text: 'Lunch with team', auto_create: false });
    expect(component.loading).toBe(false);
  });

  it('onCreateEvent confirmation flow sets parsedEvents', () => {
    apiSpy.createEventFromText.mockReturnValue(
      of({
        needs_confirmation: true,
        parsed_events: [{ title: 'X' }],
        ambiguities: ['too vague'],
      }),
    );
    component.form.setValue({ eventText: 'Lunch with team' });
    component.onCreateEvent();
    expect(component.needsConfirmation).toBe(true);
    expect(component.parsedEvents.length).toBe(1);
    expect(component.ambiguities).toEqual(['too vague']);
  });

  it('onCreateEvent failure path runs', () => {
    apiSpy.createEventFromText.mockReturnValue(of({ success: false, message: 'nope' }));
    component.form.setValue({ eventText: 'Lunch with team' });
    component.onCreateEvent();
    expect(component.loading).toBe(false);
  });

  it('onCreateEvent error path', () => {
    apiSpy.createEventFromText.mockReturnValue(throwError(() => ({ error: { detail: 'oops' } })));
    component.form.setValue({ eventText: 'Lunch with team' });
    component.onCreateEvent();
    expect(component.loading).toBe(false);
  });

  it('onConfirmEvents success resets', () => {
    apiSpy.createEventFromText.mockReturnValue(of({ success: true, created_event_ids: ['e1', 'e2'] }));
    component.form.setValue({ eventText: 'Lunch with team' });
    component.onConfirmEvents();
    expect(component.needsConfirmation).toBe(false);
    expect(component.loading).toBe(false);
  });

  it('onConfirmEvents failure path', () => {
    apiSpy.createEventFromText.mockReturnValue(of({ success: false, message: 'nope' }));
    component.form.setValue({ eventText: 'Lunch with team' });
    component.onConfirmEvents();
    expect(component.loading).toBe(false);
  });

  it('onConfirmEvents error path', () => {
    apiSpy.createEventFromText.mockReturnValue(throwError(() => ({})));
    component.form.setValue({ eventText: 'Lunch with team' });
    component.onConfirmEvents();
    expect(component.loading).toBe(false);
  });

  it('onCancelConfirmation clears state', () => {
    component.needsConfirmation = true;
    component.parsedEvents = [{ event_id: 'p1' } as never];
    component.ambiguities = ['x'];
    component.onCancelConfirmation();
    expect(component.needsConfirmation).toBe(false);
    expect(component.parsedEvents).toEqual([]);
    expect(component.ambiguities).toEqual([]);
  });

  it('formatDateTime returns a string', () => {
    const s = component.formatDateTime('2025-06-15T10:00:00Z');
    expect(typeof s).toBe('string');
    expect(s.length).toBeGreaterThan(0);
  });

  it('formatDuration handles minutes/hours/zero', () => {
    expect(component.formatDuration()).toBe('');
    expect(component.formatDuration(0)).toBe('');
    expect(component.formatDuration(45)).toBe('45m');
    expect(component.formatDuration(60)).toBe('1h');
    expect(component.formatDuration(90)).toBe('1h 30m');
  });
});
