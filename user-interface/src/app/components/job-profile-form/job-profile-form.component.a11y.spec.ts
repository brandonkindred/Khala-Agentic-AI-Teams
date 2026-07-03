import { TestBed } from '@angular/core/testing';
import { provideNoopAnimations } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { axe } from 'vitest-axe';
import { JobMatchingApiService } from '../../services/job-matching-api.service';
import { JobProfileFormComponent } from './job-profile-form.component';

// `color-contrast` is disabled because jsdom can't paint; contrast is
// enforced by the --kh-* token system + the SCSS contrast guard spec.
// `aria-required-children` is disabled because Angular Material's chip grid
// puts the `matChipInput` inside the `role="grid"` element (its documented
// API) and renders the grid role even with zero rows — both flagged by axe
// but not fixable in component markup without abandoning MatChipGrid.
const axeOptions = {
  rules: {
    'color-contrast': { enabled: false },
    'aria-required-children': { enabled: false },
  },
};

describe('JobProfileFormComponent a11y', () => {
  it('has no axe violations with the populated form', async () => {
    const apiSpy = {
      getProfile: vi.fn().mockReturnValue(
        of({
          target_titles: ['Staff Engineer'],
          seniority_levels: ['Staff'],
          locations: ['Remote (US)'],
          remote_preference: 'remote',
          salary_min: 180000,
          currency: 'USD',
          company_stages: [],
          company_sizes: [],
          industries: [],
          must_have_skills: ['Python'],
          nice_to_have_skills: [],
          deal_breakers: ['Required relocation'],
          preferred_companies: [],
          excluded_companies: [],
          work_authorization: 'US citizen',
          keywords: [],
          weights: {
            title_fit: 0.25,
            seniority_fit: 0.1,
            location_fit: 0.15,
            comp_fit: 0.15,
            company_fit: 0.15,
            skills_fit: 0.2,
          },
        })
      ),
      saveProfile: vi.fn(),
    };
    await TestBed.configureTestingModule({
      imports: [JobProfileFormComponent],
      providers: [
        provideNoopAnimations(),
        provideRouter([]),
        { provide: JobMatchingApiService, useValue: apiSpy },
      ],
    }).compileComponents();
    const fixture = TestBed.createComponent(JobProfileFormComponent);
    fixture.detectChanges();

    // Guards: chip grids and the weight sliders are actually in the DOM.
    expect(fixture.nativeElement.querySelectorAll('mat-chip-grid').length).toBeGreaterThan(0);
    expect(fixture.nativeElement.querySelectorAll('mat-slider').length).toBe(6);

    const results = await axe(fixture.nativeElement, axeOptions);
    expect(results).toHaveNoViolations();
  }, 15000);
});
