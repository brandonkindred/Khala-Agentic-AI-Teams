import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { SimpleChange } from '@angular/core';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { AccessibilityApiService } from '../../services/accessibility-api.service';
import { AccessibilityFindingsComponent } from './accessibility-findings.component';

describe('AccessibilityFindingsComponent', () => {
  let component: AccessibilityFindingsComponent;
  let fixture: ComponentFixture<AccessibilityFindingsComponent>;
  let apiSpy: { getFindings: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    apiSpy = {
      getFindings: vi.fn().mockReturnValue(
        of({ findings: [], by_severity: {}, by_issue_type: {}, total: 0 }),
      ),
    };
    await TestBed.configureTestingModule({
      imports: [AccessibilityFindingsComponent, NoopAnimationsModule],
      providers: [{ provide: AccessibilityApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(AccessibilityFindingsComponent);
    component = fixture.componentInstance;
  });

  it('creates', () => {
    expect(component).toBeTruthy();
  });

  it('ngOnChanges triggers loadFindings on new auditId', () => {
    component.auditId = 'a1';
    component.ngOnChanges({ auditId: new SimpleChange(null, 'a1', true) });
    expect(apiSpy.getFindings).toHaveBeenCalledWith('a1', {});
  });

  it('ngOnChanges ignored without auditId', () => {
    component.auditId = null;
    component.ngOnChanges({ auditId: new SimpleChange('x', null, false) as never });
    expect(apiSpy.getFindings).not.toHaveBeenCalled();
  });

  it('loadFindings passes severity and issue_type filters', () => {
    component.auditId = 'a1';
    component.severityFilter = ['Critical'];
    component.issueTypeFilter = ['focus'];
    component.loadFindings();
    expect(apiSpy.getFindings).toHaveBeenCalledWith('a1', {
      severity: ['Critical'],
      issue_type: ['focus'],
    });
  });

  it('loadFindings sets error on failure', () => {
    apiSpy.getFindings.mockReturnValue(throwError(() => ({ message: 'boom' })));
    component.auditId = 'a1';
    component.loadFindings();
    expect(component.error).toBe('boom');
    expect(component.loading).toBe(false);
  });

  it('loadFindings exits early when no auditId', () => {
    component.auditId = null;
    component.loadFindings();
    expect(apiSpy.getFindings).not.toHaveBeenCalled();
  });

  it('applyFilters reloads findings', () => {
    component.auditId = 'a1';
    component.applyFilters();
    expect(apiSpy.getFindings).toHaveBeenCalled();
  });

  it('clearFilters resets filters and reloads', () => {
    component.auditId = 'a1';
    component.severityFilter = ['Critical'];
    component.issueTypeFilter = ['focus'];
    component.clearFilters();
    expect(component.severityFilter).toEqual([]);
    expect(component.issueTypeFilter).toEqual([]);
  });

  it('hasActiveFilters reflects state', () => {
    expect(component.hasActiveFilters).toBe(false);
    component.severityFilter = ['Critical'];
    expect(component.hasActiveFilters).toBe(true);
  });

  it('toggleSelection + isSelected', () => {
    expect(component.isSelected('x')).toBe(false);
    component.toggleSelection('x');
    expect(component.isSelected('x')).toBe(true);
    component.toggleSelection('x');
    expect(component.isSelected('x')).toBe(false);
  });

  it('selectAll selects every filtered finding', () => {
    component.filteredFindings = [{ id: 'a' }, { id: 'b' }] as never;
    component.selectAll();
    expect(component.selectedCount).toBe(2);
  });

  it('clearSelection empties selection', () => {
    component.filteredFindings = [{ id: 'a' }] as never;
    component.selectAll();
    component.clearSelection();
    expect(component.selectedCount).toBe(0);
  });

  it('onRetest emits selected ids', () => {
    component.filteredFindings = [{ id: 'a' }, { id: 'b' }] as never;
    component.selectAll();
    const spy = vi.fn();
    component.retestRequested.subscribe(spy);
    component.onRetest();
    expect(spy).toHaveBeenCalledWith(['a', 'b']);
  });

  it('onRetest does nothing with empty selection', () => {
    const spy = vi.fn();
    component.retestRequested.subscribe(spy);
    component.onRetest();
    expect(spy).not.toHaveBeenCalled();
  });

  it('onExport emits format', () => {
    const spy = vi.fn();
    component.exportRequested.subscribe(spy);
    component.onExport('csv');
    expect(spy).toHaveBeenCalledWith('csv');
  });

  it('getSeverityClass lowercases', () => {
    expect(component.getSeverityClass('Critical')).toBe('critical');
  });

  it('getSeverityIcon maps severity', () => {
    expect(component.getSeverityIcon('Critical')).toBe('error');
    expect(component.getSeverityIcon('High')).toBe('warning');
    expect(component.getSeverityIcon('Medium')).toBe('info');
    expect(component.getSeverityIcon('Low')).toBe('help_outline');
    expect(component.getSeverityIcon('Bizarre' as never)).toBe('help');
  });

  it('getIssueTypeLabel humanises', () => {
    expect(component.getIssueTypeLabel('name_role_value' as never)).toBe('Name Role Value');
  });

  it('getWcagCriteria joins sc list', () => {
    expect(
      component.getWcagCriteria({ wcag_mappings: [{ sc: '1.4.3' }, { sc: '2.1.1' }] } as never),
    ).toBe('1.4.3, 2.1.1');
    expect(component.getWcagCriteria({ wcag_mappings: [] } as never)).toBe('N/A');
  });

  it('trackByFindingId returns id', () => {
    expect(component.trackByFindingId(0, { id: 'x' } as never)).toBe('x');
  });
});
