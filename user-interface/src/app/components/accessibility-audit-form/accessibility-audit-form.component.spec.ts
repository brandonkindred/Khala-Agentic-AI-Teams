import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import type { MatChipInputEvent } from '@angular/material/chips';
import { AccessibilityAuditFormComponent } from './accessibility-audit-form.component';

describe('AccessibilityAuditFormComponent', () => {
  let component: AccessibilityAuditFormComponent;
  let fixture: ComponentFixture<AccessibilityAuditFormComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AccessibilityAuditFormComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(AccessibilityAuditFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('creates with defaults', () => {
    expect(component).toBeTruthy();
    expect(component.auditType).toBe('webpage');
    expect(component.wcagLevelA).toBe(true);
    expect(component.wcagLevelAA).toBe(true);
    expect(component.wcagLevelAAA).toBe(false);
  });

  it('canSubmit returns false when empty', () => {
    expect(component.canSubmit).toBe(false);
  });

  it('canSubmit requires webUrls for non-mobile', () => {
    component.auditName = 'Test';
    expect(component.canSubmit).toBe(false);
    component.webUrls = ['https://x'];
    expect(component.canSubmit).toBe(true);
  });

  it('canSubmit honors typed URL when not yet added', () => {
    component.auditName = 'Test';
    component.webUrl = 'https://x';
    expect(component.canSubmit).toBe(true);
  });

  it('canSubmit requires mobile apps for mobile audit', () => {
    component.auditName = 'Test';
    component.auditType = 'mobile';
    expect(component.canSubmit).toBe(false);
    component.mobileApps = [{ platform: 'ios', name: 'App', version: '1.0' }];
    expect(component.canSubmit).toBe(true);
  });

  it('selectedWcagLevels returns checked levels', () => {
    component.wcagLevelA = false;
    component.wcagLevelAAA = true;
    expect(component.selectedWcagLevels).toEqual(['AA', 'AAA']);
  });

  it('addUrl appends a URL and clears input', () => {
    component.webUrl = '  https://example.com  ';
    component.addUrl();
    expect(component.webUrls).toEqual(['https://example.com']);
    expect(component.webUrl).toBe('');
  });

  it('addUrl does not add duplicates', () => {
    component.webUrls = ['https://x'];
    component.webUrl = 'https://x';
    component.addUrl();
    expect(component.webUrls).toEqual(['https://x']);
  });

  it('removeUrl filters out match', () => {
    component.webUrls = ['a', 'b'];
    component.removeUrl('a');
    expect(component.webUrls).toEqual(['b']);
  });

  it('addMobileApp pushes app then clears fields', () => {
    component.mobileAppName = 'X';
    component.mobileAppVersion = '1.0';
    component.mobileAppBuild = '5';
    component.addMobileApp();
    expect(component.mobileApps).toEqual([
      { platform: 'ios', name: 'X', version: '1.0', build: '5' },
    ]);
    expect(component.mobileAppName).toBe('');
  });

  it('addMobileApp leaves build undefined when empty', () => {
    component.mobileAppName = 'X';
    component.mobileAppVersion = '1.0';
    component.addMobileApp();
    expect(component.mobileApps[0].build).toBeUndefined();
  });

  it('addMobileApp ignores when name/version blank', () => {
    component.mobileAppName = '';
    component.mobileAppVersion = '1.0';
    component.addMobileApp();
    expect(component.mobileApps).toEqual([]);
  });

  it('removeMobileApp splices', () => {
    component.mobileApps = [
      { platform: 'ios', name: 'A', version: '1' },
      { platform: 'android', name: 'B', version: '2' },
    ];
    component.removeMobileApp(0);
    expect(component.mobileApps.length).toBe(1);
    expect(component.mobileApps[0].name).toBe('B');
  });

  it('addJourney adds value and clears chip input', () => {
    const clear = vi.fn();
    const evt = { value: '  Login flow  ', chipInput: { clear } } as unknown as MatChipInputEvent;
    component.addJourney(evt);
    expect(component.criticalJourneys).toEqual(['Login flow']);
    expect(clear).toHaveBeenCalled();
  });

  it('addJourney ignores empty', () => {
    const clear = vi.fn();
    component.addJourney({ value: '', chipInput: { clear } } as unknown as MatChipInputEvent);
    expect(component.criticalJourneys).toEqual([]);
  });

  it('addJourney avoids duplicates', () => {
    component.criticalJourneys = ['A'];
    const clear = vi.fn();
    component.addJourney({ value: 'A', chipInput: { clear } } as unknown as MatChipInputEvent);
    expect(component.criticalJourneys).toEqual(['A']);
    expect(clear).toHaveBeenCalled();
  });

  it('removeJourney filters value', () => {
    component.criticalJourneys = ['A', 'B'];
    component.removeJourney('A');
    expect(component.criticalJourneys).toEqual(['B']);
  });

  it('onSubmit emits CreateAuditRequest for web', () => {
    const spy = vi.fn();
    component.submitRequest.subscribe(spy);
    component.auditName = 'My Audit';
    component.webUrls = ['https://x'];
    component.timeboxHours = 4;
    component.maxPages = 10;
    component.onSubmit();
    expect(spy).toHaveBeenCalled();
    const req = spy.mock.calls[0][0];
    expect(req.name).toBe('My Audit');
    expect(req.web_urls).toEqual(['https://x']);
    expect(req.timebox_hours).toBe(4);
    expect(req.max_pages).toBe(10);
  });

  it('onSubmit picks effective webUrls including typed', () => {
    const spy = vi.fn();
    component.submitRequest.subscribe(spy);
    component.auditName = 'A';
    component.webUrls = ['https://a'];
    component.webUrl = 'https://b';
    component.onSubmit();
    const req = spy.mock.calls[0][0];
    expect(req.web_urls).toEqual(['https://a', 'https://b']);
  });

  it('onSubmit for mobile sets mobile_apps + skips web', () => {
    const spy = vi.fn();
    component.submitRequest.subscribe(spy);
    component.auditName = 'A';
    component.auditType = 'mobile';
    component.mobileApps = [{ platform: 'ios', name: 'X', version: '1.0' }];
    component.onSubmit();
    const req = spy.mock.calls[0][0];
    expect(req.web_urls).toEqual([]);
    expect(req.mobile_apps.length).toBe(1);
    expect(req.tech_stack.web).toBe('other');
    expect(req.tech_stack.mobile).toBe('native');
  });

  it('onSubmit for SPA sets tech_stack.web=spa', () => {
    const spy = vi.fn();
    component.submitRequest.subscribe(spy);
    component.auditName = 'A';
    component.auditType = 'spa';
    component.webUrls = ['https://x'];
    component.onSubmit();
    const req = spy.mock.calls[0][0];
    expect(req.tech_stack.web).toBe('spa');
  });

  it('onSubmit does nothing when canSubmit is false', () => {
    const spy = vi.fn();
    component.submitRequest.subscribe(spy);
    component.onSubmit();
    expect(spy).not.toHaveBeenCalled();
  });

  it('resetForm clears state', () => {
    component.auditName = 'A';
    component.webUrls = ['x'];
    component.mobileApps = [{ platform: 'ios', name: 'A', version: '1' }];
    component.criticalJourneys = ['J'];
    component.maxPages = 5;
    component.timeboxHours = 3;
    component.resetForm();
    expect(component.auditName).toBe('');
    expect(component.webUrls).toEqual([]);
    expect(component.mobileApps).toEqual([]);
    expect(component.criticalJourneys).toEqual([]);
    expect(component.maxPages).toBeNull();
    expect(component.timeboxHours).toBeNull();
  });
});
