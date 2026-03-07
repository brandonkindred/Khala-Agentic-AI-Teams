import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { AccessibilityReportComponent } from './accessibility-report.component';

describe('AccessibilityReportComponent', () => {
  let component: AccessibilityReportComponent;
  let fixture: ComponentFixture<AccessibilityReportComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AccessibilityReportComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(AccessibilityReportComponent);
    component = fixture.componentInstance;
    component.auditId = 'a1';
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
