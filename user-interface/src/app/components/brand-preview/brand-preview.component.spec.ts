import { ComponentFixture, TestBed } from '@angular/core/testing';
import { BrandPreviewComponent } from './brand-preview.component';

describe('BrandPreviewComponent', () => {
  let component: BrandPreviewComponent;
  let fixture: ComponentFixture<BrandPreviewComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [BrandPreviewComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(BrandPreviewComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should show empty state when no output and no mission data', () => {
    component.latestOutput = null;
    component.mission = null;
    fixture.detectChanges();
    expect(component.hasOutput).toBe(false);
    expect(component.hasMissionData).toBe(false);
    expect(component.hasContent).toBe(false);
  });

  it('should show live preview when mission has data but no output', () => {
    component.latestOutput = null;
    component.mission = {
      company_name: 'Acme Corp',
      company_description: 'We build rockets',
      target_audience: 'Space enthusiasts',
      values: ['innovation', 'boldness'],
    };
    fixture.detectChanges();
    expect(component.hasOutput).toBe(false);
    expect(component.hasMissionData).toBe(true);
    expect(component.hasContent).toBe(true);
  });

  it('should not show live preview for default TBD mission', () => {
    component.latestOutput = null;
    component.mission = {
      company_name: 'TBD',
      company_description: 'To be discussed.',
      target_audience: 'TBD',
    };
    fixture.detectChanges();
    expect(component.hasMissionData).toBe(false);
    expect(component.hasContent).toBe(false);
  });

  it('should show full output tabs when latestOutput is set', () => {
    component.latestOutput = {
      status: 'needs_human_decision',
      mission_summary: 'Test summary',
      brand_guidelines: [],
    };
    fixture.detectChanges();
    expect(component.hasOutput).toBe(true);
    expect(component.hasContent).toBe(true);
  });

  it('should detect selected palette from mission', () => {
    component.mission = {
      company_name: 'Test',
      company_description: 'Test company',
      target_audience: 'Testers',
      color_palettes: [
        { name: 'Sunset', description: 'Warm tones', colors: ['orange', 'coral'], sentiment: 'warm' },
        { name: 'Ocean', description: 'Cool tones', colors: ['blue', 'teal'], sentiment: 'cool' },
      ],
      selected_palette_index: 1,
    };
    fixture.detectChanges();
    expect(component.selectedPalette).toEqual(
      { name: 'Ocean', description: 'Cool tones', colors: ['blue', 'teal'], sentiment: 'cool' }
    );
    expect(component.isPaletteSelected(0)).toBe(false);
    expect(component.isPaletteSelected(1)).toBe(true);
  });

  it('should return null selectedPalette when no index is set', () => {
    component.mission = {
      company_name: 'Test',
      company_description: 'Test company',
      target_audience: 'Testers',
      color_palettes: [
        { name: 'Sunset', description: 'Warm', colors: ['orange'], sentiment: 'warm' },
      ],
    };
    fixture.detectChanges();
    expect(component.selectedPalette).toBeNull();
  });
});
