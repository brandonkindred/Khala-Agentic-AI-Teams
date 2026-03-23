import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import { PersonalAssistantDashboardComponent } from './personal-assistant-dashboard.component';

describe('PersonalAssistantDashboardComponent', () => {
  let component: PersonalAssistantDashboardComponent;
  let fixture: ComponentFixture<PersonalAssistantDashboardComponent>;

  beforeEach(async () => {
    const apiSpy = {
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
      getProfile: vi.fn().mockReturnValue(of({})),
      updateProfile: vi.fn().mockReturnValue(of({})),
      getTasks: vi.fn().mockReturnValue(of([])),
      addTasksFromText: vi.fn().mockReturnValue(of({})),
      updateTaskItem: vi.fn().mockReturnValue(of({})),
      getWishlist: vi.fn().mockReturnValue(of([])),
      removeFromWishlist: vi.fn().mockReturnValue(of({})),
      searchDeals: vi.fn().mockReturnValue(of([])),
      getReservations: vi.fn().mockReturnValue(of([])),
      createReservationFromText: vi.fn().mockReturnValue(of({})),
      getDocuments: vi.fn().mockReturnValue(of([])),
      sendMessage: vi.fn().mockReturnValue(of({})),
      createEventFromText: vi.fn().mockReturnValue(of({})),
    };
    await TestBed.configureTestingModule({
      imports: [PersonalAssistantDashboardComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: PersonalAssistantApiService, useValue: apiSpy },
      ],
    }).compileComponents();

    localStorage.removeItem('pa_user_id');
    fixture = TestBed.createComponent(PersonalAssistantDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  afterEach(() => localStorage.removeItem('pa_user_id'));

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should default userId to default when no stored value', () => {
    expect(component.userId).toBe('default');
  });

  it('should read userId from localStorage on init', () => {
    localStorage.setItem('pa_user_id', 'user-123');
    const f = TestBed.createComponent(PersonalAssistantDashboardComponent);
    const c = f.componentInstance;
    c.ngOnInit();
    expect(c.userId).toBe('user-123');
  });

  it('onUserIdChange updates userId and localStorage', () => {
    component.onUserIdChange('new-user');
    expect(component.userId).toBe('new-user');
    expect(localStorage.getItem('pa_user_id')).toBe('new-user');
  });

  it('healthCheck calls api.health', () => {
    const api = TestBed.inject(PersonalAssistantApiService) as { health: ReturnType<typeof vi.fn> };
    component.healthCheck().subscribe((r) => expect(r).toEqual({ status: 'ok' }));
    expect(api.health).toHaveBeenCalled();
  });
});
