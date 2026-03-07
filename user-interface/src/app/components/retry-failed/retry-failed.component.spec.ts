import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { RetryFailedComponent } from './retry-failed.component';

describe('RetryFailedComponent', () => {
  let component: RetryFailedComponent;
  let fixture: ComponentFixture<RetryFailedComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [RetryFailedComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(RetryFailedComponent);
    component = fixture.componentInstance;
    component.jobId = 'j1';
    component.hasFailedTasks = true;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should emit retry when retry action triggered', (done) => {
    component.retry.subscribe(() => {
      expect(true).toBe(true);
      done();
    });
    fixture.nativeElement.querySelector('button')?.click();
  });
});
