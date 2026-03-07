import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { AccessibilityApiService } from '../../services/accessibility-api.service';
import { AccessibilityFindingsComponent } from './accessibility-findings.component';

describe('AccessibilityFindingsComponent', () => {
  let component: AccessibilityFindingsComponent;
  let fixture: ComponentFixture<AccessibilityFindingsComponent>;

  beforeEach(async () => {
    const apiSpy = { getFindings: vi.fn().mockReturnValue(of({ findings: [] })) };
    await TestBed.configureTestingModule({
      imports: [AccessibilityFindingsComponent, NoopAnimationsModule],
      providers: [{ provide: AccessibilityApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(AccessibilityFindingsComponent);
    component = fixture.componentInstance;
    component.auditId = 'a1';
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
