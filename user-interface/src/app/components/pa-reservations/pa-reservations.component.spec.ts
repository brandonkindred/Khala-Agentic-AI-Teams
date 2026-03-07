import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import { PaReservationsComponent } from './pa-reservations.component';

describe('PaReservationsComponent', () => {
  let component: PaReservationsComponent;
  let fixture: ComponentFixture<PaReservationsComponent>;

  beforeEach(async () => {
    const apiSpy = { getReservations: vi.fn().mockReturnValue(of([])) };
    await TestBed.configureTestingModule({
      imports: [PaReservationsComponent, NoopAnimationsModule],
      providers: [{ provide: PersonalAssistantApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(PaReservationsComponent);
    component = fixture.componentInstance;
    component.userId = 'u1';
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
