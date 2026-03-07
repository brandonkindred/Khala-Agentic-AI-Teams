import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { SocialMarketingPerformanceComponent } from './social-marketing-performance.component';

describe('SocialMarketingPerformanceComponent', () => {
  let component: SocialMarketingPerformanceComponent;
  let fixture: ComponentFixture<SocialMarketingPerformanceComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [SocialMarketingPerformanceComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(SocialMarketingPerformanceComponent);
    component = fixture.componentInstance;
    component.jobId = 'j1';
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
