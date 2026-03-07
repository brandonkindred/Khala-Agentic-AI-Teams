import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { FrontendCodeV2RunFormComponent } from './frontend-code-v2-run-form.component';

describe('FrontendCodeV2RunFormComponent', () => {
  let component: FrontendCodeV2RunFormComponent;
  let fixture: ComponentFixture<FrontendCodeV2RunFormComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [FrontendCodeV2RunFormComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(FrontendCodeV2RunFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
