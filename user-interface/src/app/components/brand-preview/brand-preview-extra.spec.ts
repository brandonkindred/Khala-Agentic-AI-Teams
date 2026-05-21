import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideNoopAnimations } from '@angular/platform-browser/animations';
import { vi, beforeEach } from 'vitest';
import { BrandPreviewComponent } from './brand-preview.component';
import type { BrandingTeamOutput } from '../../models';

describe('BrandPreviewComponent (extra coverage)', () => {
  let component: BrandPreviewComponent;
  let fixture: ComponentFixture<BrandPreviewComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [BrandPreviewComponent],
      providers: [provideNoopAnimations()],
    }).compileComponents();
    fixture = TestBed.createComponent(BrandPreviewComponent);
    component = fixture.componentInstance;
  });

  it('openBrandBook ignores call when no content', () => {
    component.latestOutput = null;
    component.openBrandBook();
    expect(component.brandBookOpen).toBe(false);
    component.latestOutput = { brand_book: null } as unknown as BrandingTeamOutput;
    component.openBrandBook();
    expect(component.brandBookOpen).toBe(false);
  });

  it('openBrandBook sets flag when content present', () => {
    component.latestOutput = { brand_book: { content: '# Brand book' } } as unknown as BrandingTeamOutput;
    component.openBrandBook();
    expect(component.brandBookOpen).toBe(true);
  });

  it('closeBrandBook clears the flag', () => {
    component.brandBookOpen = true;
    component.closeBrandBook();
    expect(component.brandBookOpen).toBe(false);
  });

  it('downloadBrandBook no-ops without content', () => {
    component.latestOutput = null;
    component.downloadBrandBook();
    component.latestOutput = { brand_book: { content: '' } } as unknown as BrandingTeamOutput;
    component.downloadBrandBook();
  });

  it('downloadBrandBook builds a download anchor when content present', () => {
    component.latestOutput = { brand_book: { content: '# Hi' } } as unknown as BrandingTeamOutput;
    (window.URL as unknown as { createObjectURL: (b: Blob) => string }).createObjectURL = vi.fn(() => 'blob://x');
    (window.URL as unknown as { revokeObjectURL: (s: string) => void }).revokeObjectURL = vi.fn();
    const clickSpy = vi.fn();
    const fakeAnchor = {
      click: clickSpy,
      href: '',
      download: '',
    } as unknown as HTMLAnchorElement;
    const createSpy = vi.spyOn(document, 'createElement').mockReturnValue(fakeAnchor);
    const appendSpy = vi.spyOn(document.body, 'appendChild').mockImplementation((el) => el);
    const removeSpy = vi.spyOn(document.body, 'removeChild').mockImplementation((el) => el);
    component.downloadBrandBook();
    expect(clickSpy).toHaveBeenCalled();
    expect(appendSpy).toHaveBeenCalled();
    expect(removeSpy).toHaveBeenCalled();
    createSpy.mockRestore();
    appendSpy.mockRestore();
    removeSpy.mockRestore();
  });
});
