import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { SimpleChange } from '@angular/core';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import { PaReservationsComponent } from './pa-reservations.component';

describe('PaReservationsComponent', () => {
  let component: PaReservationsComponent;
  let fixture: ComponentFixture<PaReservationsComponent>;
  let apiSpy: {
    getReservations: ReturnType<typeof vi.fn>;
    createReservationFromText: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    apiSpy = {
      getReservations: vi.fn().mockReturnValue(of([])),
      createReservationFromText: vi.fn().mockReturnValue(of({ success: true })),
    };
    await TestBed.configureTestingModule({
      imports: [PaReservationsComponent, NoopAnimationsModule],
      providers: [{ provide: PersonalAssistantApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(PaReservationsComponent);
    component = fixture.componentInstance;
    component.userId = 'u1';
    fixture.detectChanges();
  });

  it('creates and loads reservations', () => {
    expect(component).toBeTruthy();
    expect(apiSpy.getReservations).toHaveBeenCalledWith('u1');
  });

  it('loadReservations handles error', () => {
    apiSpy.getReservations.mockReturnValue(throwError(() => ({})));
    (component as unknown as { loadReservations: () => void }).loadReservations();
    expect(component.reservations).toEqual([]);
    expect(component.loading).toBe(false);
  });

  it('ngOnChanges reloads on userId change', () => {
    apiSpy.getReservations.mockClear();
    component.ngOnChanges({ userId: new SimpleChange('u1', 'u2', false) });
    expect(apiSpy.getReservations).toHaveBeenCalled();
  });

  it('ngOnChanges ignores first change', () => {
    apiSpy.getReservations.mockClear();
    component.ngOnChanges({ userId: new SimpleChange(undefined, 'u1', true) });
    expect(apiSpy.getReservations).not.toHaveBeenCalled();
  });

  it('onCreateReservation does nothing if invalid', () => {
    component.form.setValue({ reservationText: 'ab' });
    component.onCreateReservation();
    expect(apiSpy.createReservationFromText).not.toHaveBeenCalled();
  });

  it('onCreateReservation does nothing if creating', () => {
    component.form.setValue({ reservationText: 'Book dinner tomorrow' });
    component.creating = true;
    component.onCreateReservation();
    expect(apiSpy.createReservationFromText).not.toHaveBeenCalled();
  });

  it('onCreateReservation success without action', () => {
    component.form.setValue({ reservationText: 'Book dinner tomorrow' });
    component.onCreateReservation();
    expect(apiSpy.createReservationFromText).toHaveBeenCalledWith('u1', { text: 'Book dinner tomorrow' });
    expect(component.creating).toBe(false);
  });

  it('onCreateReservation pending with action_required', () => {
    apiSpy.createReservationFromText.mockReturnValue(of({ success: true, action_required: 'call vendor' }));
    component.form.setValue({ reservationText: 'Book dinner tomorrow' });
    component.onCreateReservation();
    expect(component.creating).toBe(false);
  });

  it('onCreateReservation failure path', () => {
    apiSpy.createReservationFromText.mockReturnValue(of({ success: false, message: 'nope' }));
    component.form.setValue({ reservationText: 'Book dinner tomorrow' });
    component.onCreateReservation();
    expect(component.creating).toBe(false);
  });

  it('onCreateReservation error path', () => {
    apiSpy.createReservationFromText.mockReturnValue(throwError(() => ({ error: { detail: 'oops' } })));
    component.form.setValue({ reservationText: 'Book dinner tomorrow' });
    component.onCreateReservation();
    expect(component.creating).toBe(false);
  });

  it('formatDateTime returns string', () => {
    expect(component.formatDateTime('2025-06-15T18:00:00Z').length).toBeGreaterThan(0);
  });

  it('getTypeIcon maps types', () => {
    expect(component.getTypeIcon('restaurant')).toBe('restaurant');
    expect(component.getTypeIcon('appointment')).toBe('event');
    expect(component.getTypeIcon('service')).toBe('build');
    expect(component.getTypeIcon()).toBe('bookmark');
  });

  it('getStatusColor maps statuses', () => {
    expect(component.getStatusColor('confirmed')).toBe('#3fb950');
    expect(component.getStatusColor('pending')).toBe('#d29922');
    expect(component.getStatusColor('cancelled')).toBe('#f85149');
    expect(component.getStatusColor()).toBe('#8b949e');
  });
});
