import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { BackendCodeV2RunFormComponent } from './backend-code-v2-run-form.component';

describe('BackendCodeV2RunFormComponent', () => {
  let component: BackendCodeV2RunFormComponent;
  let fixture: ComponentFixture<BackendCodeV2RunFormComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [BackendCodeV2RunFormComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(BackendCodeV2RunFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
