import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HealthIndicatorComponent } from './health-indicator.component';

describe('HealthIndicatorComponent', () => {
  let component: HealthIndicatorComponent;
  let fixture: ComponentFixture<HealthIndicatorComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [HealthIndicatorComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(HealthIndicatorComponent);
    component = fixture.componentInstance;
    component.healthCheck = () => of({ status: 'ok' });
    component.label = 'Test API';
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should set status to ok when healthCheck emits ok', () => {
    expect(component.status).toBe('ok');
  });

  it('should set status to error when healthCheck fails', async () => {
    const f = TestBed.createComponent(HealthIndicatorComponent);
    const c = f.componentInstance;
    c.healthCheck = () => of({ status: 'error' });
    c.ngOnInit();
    expect(c.status).toBe('error');
  });
});
