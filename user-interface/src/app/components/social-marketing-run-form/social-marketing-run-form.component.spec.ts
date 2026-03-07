import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { SocialMarketingRunFormComponent } from './social-marketing-run-form.component';

describe('SocialMarketingRunFormComponent', () => {
  let component: SocialMarketingRunFormComponent;
  let fixture: ComponentFixture<SocialMarketingRunFormComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [SocialMarketingRunFormComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(SocialMarketingRunFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
