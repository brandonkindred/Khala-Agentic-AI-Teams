import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { SimpleChange } from '@angular/core';
import { vi } from 'vitest';
import { AccessibilityReportComponent } from './accessibility-report.component';
import { environment } from '../../../environments/environment';

describe('AccessibilityReportComponent', () => {
  let component: AccessibilityReportComponent;
  let fixture: ComponentFixture<AccessibilityReportComponent>;
  let httpMock: HttpTestingController;
  const baseUrl = environment.accessibilityApiUrl;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AccessibilityReportComponent, NoopAnimationsModule],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();

    fixture = TestBed.createComponent(AccessibilityReportComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('creates', () => {
    expect(component).toBeTruthy();
  });

  it('ngOnChanges loads report when auditId changes', () => {
    component.auditId = 'a1';
    component.ngOnChanges({
      auditId: new SimpleChange(null, 'a1', true),
    });
    const req = httpMock.expectOne(`${baseUrl}/audit/a1/report`);
    req.flush({ total_findings: 0, by_severity: {} });
    expect(component.report).toBeDefined();
    expect(component.loading).toBe(false);
  });

  it('ngOnChanges does nothing when auditId is null', () => {
    component.auditId = null;
    component.ngOnChanges({});
    httpMock.expectNone(`${baseUrl}/audit//report`);
  });

  it('loadReport sets error on failure', () => {
    component.auditId = 'a1';
    component.loadReport();
    const req = httpMock.expectOne(`${baseUrl}/audit/a1/report`);
    req.flush('boom', { status: 500, statusText: 'Server Error' });
    expect(component.error).toBeTruthy();
    expect(component.loading).toBe(false);
  });

  it('loadReport early-exits when no auditId', () => {
    component.auditId = null;
    component.loadReport();
    httpMock.expectNone(`${baseUrl}/audit/null/report`);
    expect(component.loading).toBe(false);
  });

  it('complianceScore is 0 with no report', () => {
    expect(component.complianceScore).toBe(0);
  });

  it('complianceScore is 100 with zero findings', () => {
    component.report = {
      total_findings: 0,
      by_severity: {},
    } as never;
    expect(component.complianceScore).toBe(100);
  });

  it('complianceScore penalises critical and high severity', () => {
    component.report = {
      total_findings: 10,
      by_severity: { Critical: 1, High: 2 } as never,
    } as never;
    // critical>0: 100 - 20 - 10*2 = 60
    expect(component.complianceScore).toBe(60);
  });

  it('complianceScore handles only high severity', () => {
    component.report = {
      total_findings: 5,
      by_severity: { High: 1 } as never,
    } as never;
    // No critical: 100 - 1*10 - (5-1)*2 = 82
    expect(component.complianceScore).toBe(82);
  });

  it('complianceScore floors at 0', () => {
    component.report = {
      total_findings: 200,
      by_severity: { Critical: 10 } as never,
    } as never;
    expect(component.complianceScore).toBe(0);
  });

  it('complianceLevel reflects bands', () => {
    component.report = { total_findings: 0, by_severity: {} } as never;
    expect(component.complianceLevel).toBe('Excellent');
    component.report = { total_findings: 5, by_severity: { High: 2 } as never } as never;
    // 100 - 20 - 6 = 74
    expect(component.complianceLevel).toBe('Good');
    component.report = { total_findings: 10, by_severity: { High: 5 } as never } as never;
    // 100 - 50 - 10 = 40
    expect(component.complianceLevel).toBe('Critical Issues');
    component.report = { total_findings: 7, by_severity: { High: 4 } as never } as never;
    // 100 - 40 - 6 = 54
    expect(component.complianceLevel).toBe('Needs Work');
  });

  it('complianceColor reflects bands', () => {
    component.report = { total_findings: 0, by_severity: {} } as never;
    expect(component.complianceColor).toBe('#4caf50');
    component.report = { total_findings: 5, by_severity: { High: 2 } as never } as never;
    expect(component.complianceColor).toBe('#8bc34a');
    component.report = { total_findings: 7, by_severity: { High: 4 } as never } as never;
    expect(component.complianceColor).toBe('#ff9800');
    component.report = { total_findings: 10, by_severity: { High: 5 } as never } as never;
    expect(component.complianceColor).toBe('#f44336');
  });

  it('getSeverityClass lower-cases', () => {
    expect(component.getSeverityClass('Critical')).toBe('critical');
  });

  it('getSeverityCount falls back to 0', () => {
    component.report = { total_findings: 0, by_severity: { High: 3 } as never } as never;
    expect(component.getSeverityCount('High' as never)).toBe(3);
    expect(component.getSeverityCount('Critical' as never)).toBe(0);
  });

  it('getSeverityCount returns 0 when no report', () => {
    expect(component.getSeverityCount('High' as never)).toBe(0);
  });

  it('getPatternPriorityLabel covers bands', () => {
    expect(component.getPatternPriorityLabel(1)).toContain('P0');
    expect(component.getPatternPriorityLabel(3)).toContain('P1');
    expect(component.getPatternPriorityLabel(5)).toContain('P2');
    expect(component.getPatternPriorityLabel(9)).toContain('P3');
  });

  it('getPatternPriorityClass covers bands', () => {
    expect(component.getPatternPriorityClass(0)).toBe('priority-p0');
    expect(component.getPatternPriorityClass(2)).toBe('priority-p1');
    expect(component.getPatternPriorityClass(4)).toBe('priority-p2');
    expect(component.getPatternPriorityClass(10)).toBe('priority-p3');
  });

  it('onExport emits format', () => {
    const spy = vi.fn();
    component.exportRequested.subscribe(spy);
    component.onExport('csv');
    expect(spy).toHaveBeenCalledWith('csv');
  });

  it('onViewFindings emits', () => {
    const spy = vi.fn();
    component.viewFindings.subscribe(spy);
    component.onViewFindings();
    expect(spy).toHaveBeenCalled();
  });

  it('trackByPatternId returns pattern_id', () => {
    expect(component.trackByPatternId(0, { pattern_id: 'p1' } as never)).toBe('p1');
  });
});
