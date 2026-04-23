import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideNoopAnimations } from '@angular/platform-browser/animations';
import { BrandPreviewComponent } from './brand-preview.component';
import type { BrandingTeamOutput } from '../../models';

describe('BrandPreviewComponent', () => {
  let component: BrandPreviewComponent;
  let fixture: ComponentFixture<BrandPreviewComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [BrandPreviewComponent],
      providers: [provideNoopAnimations()],
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

  it('should show the stepper when mission has data but no output', () => {
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
    const stepper = fixture.nativeElement.querySelector('[data-testid="phase-stepper"]');
    expect(stepper).not.toBeNull();
  });

  it('should not show the stepper for default TBD mission', () => {
    component.latestOutput = null;
    component.mission = {
      company_name: 'TBD',
      company_description: 'To be discussed.',
      target_audience: 'TBD',
    };
    fixture.detectChanges();
    expect(component.hasMissionData).toBe(false);
    expect(component.hasContent).toBe(false);
    const stepper = fixture.nativeElement.querySelector('[data-testid="phase-stepper"]');
    expect(stepper).toBeNull();
  });

  it('should render all five pipeline phases when latestOutput is set', () => {
    component.latestOutput = {
      status: 'needs_human_decision',
      mission_summary: 'Test summary',
      brand_guidelines: [],
    };
    fixture.detectChanges();
    expect(component.hasOutput).toBe(true);
    const steps = fixture.nativeElement.querySelectorAll('.phase-step');
    expect(steps.length).toBe(5);
    const phases = Array.from(steps).map((el) => (el as HTMLElement).getAttribute('data-phase'));
    expect(phases).toEqual([
      'strategic_core',
      'narrative_messaging',
      'visual_identity',
      'channel_activation',
      'governance',
    ]);
  });

  it('should no longer render the old progress bar', () => {
    component.latestOutput = null;
    component.mission = {
      company_name: 'Acme Corp',
      company_description: 'We build rockets',
      target_audience: 'Humans',
      values: ['focus'],
    };
    fixture.detectChanges();
    const progressTrack = fixture.nativeElement.querySelector('.progress-track');
    expect(progressTrack).toBeNull();
  });

  it('should reflect phase_gates in the status chips', () => {
    component.latestOutput = {
      status: 'in_progress',
      mission_summary: 'Summary',
      brand_guidelines: [],
      phase_gates: [
        { phase: 'strategic_core', status: 'approved' },
        { phase: 'narrative_messaging', status: 'in_progress' },
        { phase: 'visual_identity', status: 'not_started' },
      ],
    };
    fixture.detectChanges();
    expect(component.phaseStatus('strategic_core')).toBe('completed');
    expect(component.phaseStatus('narrative_messaging')).toBe('in_progress');
    expect(component.phaseStatus('visual_identity')).toBe('not_started');
    expect(component.phaseStatus('channel_activation')).toBe('not_started');
    const chips = fixture.nativeElement.querySelectorAll('.phase-chip');
    expect(chips.length).toBe(5);
    expect((chips[0] as HTMLElement).textContent?.trim()).toBe('Completed');
    expect((chips[1] as HTMLElement).textContent?.trim()).toBe('In progress');
    expect((chips[2] as HTMLElement).textContent?.trim()).toBe('Not started');
  });

  it('should fall back to content heuristic when phase_gates is absent', () => {
    component.latestOutput = {
      status: 'in_progress',
      mission_summary: 'Summary',
      codification: {
        positioning_statement: 'p',
        brand_promise: 'b',
        brand_personality_traits: [],
        narrative_pillars: [],
      },
      brand_guidelines: [],
    };
    fixture.detectChanges();
    expect(component.phaseStatus('strategic_core')).toBe('completed');
    expect(component.phaseStatus('narrative_messaging')).toBe('not_started');
  });

  it('should hide the brand-book button when no content is present', () => {
    component.latestOutput = {
      status: 'in_progress',
      mission_summary: 'Summary',
      brand_guidelines: [],
    };
    fixture.detectChanges();
    const btn = fixture.nativeElement.querySelector('[data-testid="brand-book-btn"]');
    expect(btn).toBeNull();
  });

  it('should show the brand-book button and open the overlay when content is present', () => {
    component.latestOutput = {
      status: 'complete',
      mission_summary: 'Summary',
      brand_guidelines: [],
      brand_book: { content: '# Brand Book\n\nFull doc here.' },
    };
    fixture.detectChanges();
    const btn = fixture.nativeElement.querySelector(
      '[data-testid="brand-book-btn"]'
    ) as HTMLButtonElement;
    expect(btn).not.toBeNull();
    btn.click();
    fixture.detectChanges();
    expect(component.brandBookOpen).toBe(true);
    const overlay = fixture.nativeElement.querySelector(
      '[data-testid="brand-book-overlay"]'
    );
    expect(overlay).not.toBeNull();
    expect((overlay as HTMLElement).textContent).toContain('# Brand Book');
  });

  it('should emit selectPalette when a palette card button is clicked', () => {
    const emitted: number[] = [];
    component.selectPalette.subscribe((i) => emitted.push(i));
    component.mission = {
      company_name: 'Test',
      company_description: 'Test',
      target_audience: 'Test',
      color_palettes: [
        { name: 'Sunset', description: 'Warm', colors: ['orange'], sentiment: 'warm' },
        { name: 'Ocean', description: 'Cool', colors: ['blue'], sentiment: 'cool' },
      ],
    };
    // latestOutput needed to render visual_identity phase body (phase_gates
    // absent → falls back to phaseHasOutput, which checks missionPalettes).
    component.latestOutput = {
      status: 'in_progress',
      mission_summary: 'Summary',
      brand_guidelines: [],
    } as BrandingTeamOutput;
    fixture.detectChanges();
    const buttons = fixture.nativeElement.querySelectorAll(
      '[data-testid="palette-select-btn"]'
    );
    expect(buttons.length).toBe(2);
    (buttons[1] as HTMLButtonElement).click();
    expect(emitted).toEqual([1]);
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
