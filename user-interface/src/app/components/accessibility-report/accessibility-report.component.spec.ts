import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { AccessibilityReportComponent } from './accessibility-report.component';

describe('AccessibilityReportComponent', () => {
  let component: AccessibilityReportComponent;
  let fixture: ComponentFixture<AccessibilityReportComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AccessibilityReportComponent, NoopAnimationsModule],
      providers: [provideHttpClient(), provideHttpClientTesting()],
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
