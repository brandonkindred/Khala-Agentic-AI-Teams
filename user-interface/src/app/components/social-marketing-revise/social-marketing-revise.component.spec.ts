import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { SocialMarketingReviseComponent } from './social-marketing-revise.component';

describe('SocialMarketingReviseComponent', () => {
  let component: SocialMarketingReviseComponent;
  let fixture: ComponentFixture<SocialMarketingReviseComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [SocialMarketingReviseComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(SocialMarketingReviseComponent);
    component = fixture.componentInstance;
    component.jobId = 'j1';
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
