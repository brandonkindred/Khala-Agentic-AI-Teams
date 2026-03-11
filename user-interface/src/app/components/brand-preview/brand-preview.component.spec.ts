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

  it('should show empty state when latestOutput is null', () => {
    component.latestOutput = null;
    fixture.detectChanges();
    expect(component.hasOutput).toBe(false);
  });

  it('should show content when latestOutput is set', () => {
    component.latestOutput = {
      status: 'needs_human_decision',
      mission_summary: 'Test summary',
      brand_guidelines: [],
    };
    fixture.detectChanges();
    expect(component.hasOutput).toBe(true);
  });
});
