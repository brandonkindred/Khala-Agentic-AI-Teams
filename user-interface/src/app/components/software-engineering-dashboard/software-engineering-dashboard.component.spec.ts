import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { provideHttpClient } from '@angular/common/http';
import { ActivatedRoute } from '@angular/router';
import { vi } from 'vitest';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { SoftwareEngineeringDashboardComponent } from './software-engineering-dashboard.component';

describe('SoftwareEngineeringDashboardComponent', () => {
  let component: SoftwareEngineeringDashboardComponent;
  let fixture: ComponentFixture<SoftwareEngineeringDashboardComponent>;
  let apiSpy: { health: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    apiSpy = {
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
    };

    await TestBed.configureTestingModule({
      imports: [SoftwareEngineeringDashboardComponent],
      providers: [
        provideHttpClient(),
        { provide: SoftwareEngineeringApiService, useValue: apiSpy },
        { provide: ActivatedRoute, useValue: {} },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(SoftwareEngineeringDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('healthCheck should call api.health', () => {
    component.healthCheck().subscribe((r) => expect(r).toEqual({ status: 'ok' }));
    expect(apiSpy.health).toHaveBeenCalled();
  });
});
