import { ComponentFixture, TestBed } from '@angular/core/testing';
import { MatSnackBar } from '@angular/material/snack-bar';
import { provideNoopAnimations } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { JobMatchingApiService } from '../../services/job-matching-api.service';
import { JobProfileFormComponent } from './job-profile-form.component';
import type { JobSeekerProfile } from '../../models';

function makeProfile(overrides: Partial<JobSeekerProfile> = {}): JobSeekerProfile {
  return {
    target_titles: ['Staff Engineer'],
    seniority_levels: ['Staff'],
    locations: ['Remote (US)'],
    remote_preference: 'remote',
    salary_min: 180000,
    currency: 'USD',
    company_stages: ['Series B'],
    company_sizes: ['51-200'],
    industries: ['AI / ML'],
    must_have_skills: ['Python'],
    nice_to_have_skills: ['Go'],
    deal_breakers: ['Required relocation'],
    preferred_companies: ['Anthropic'],
    excluded_companies: ['Bad Co'],
    work_authorization: 'US citizen',
    keywords: ['platform'],
    weights: {
      title_fit: 0.3,
      seniority_fit: 0.1,
      location_fit: 0.15,
      comp_fit: 0.15,
      company_fit: 0.1,
      skills_fit: 0.2,
    },
    ...overrides,
  };
}

describe('JobProfileFormComponent', () => {
  let fixture: ComponentFixture<JobProfileFormComponent>;
  let component: JobProfileFormComponent;
  let apiSpy: {
    getProfile: ReturnType<typeof vi.fn>;
    saveProfile: ReturnType<typeof vi.fn>;
  };
  let snackSpy: { open: ReturnType<typeof vi.fn> };

  async function setup(profile: JobSeekerProfile = makeProfile()): Promise<void> {
    apiSpy = {
      getProfile: vi.fn().mockReturnValue(of(profile)),
      saveProfile: vi.fn(),
    };
    snackSpy = { open: vi.fn() };
    await TestBed.configureTestingModule({
      imports: [JobProfileFormComponent],
      providers: [
        provideNoopAnimations(),
        provideRouter([]),
        { provide: JobMatchingApiService, useValue: apiSpy },
        { provide: MatSnackBar, useValue: snackSpy },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(JobProfileFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  }

  it('loads the profile into chips and form controls', async () => {
    await setup();
    expect(apiSpy.getProfile).toHaveBeenCalled();
    expect(component.chips.target_titles).toEqual(['Staff Engineer']);
    expect(component.chips.deal_breakers).toEqual(['Required relocation']);
    expect(component.form.getRawValue().remote_preference).toBe('remote');
    expect(component.form.getRawValue().salary_min).toBe(180000);
    expect(component.form.getRawValue().title_fit).toBe(0.3);
  });

  it('surfaces a load error with retry', async () => {
    apiSpy = {
      getProfile: vi.fn().mockReturnValue(throwError(() => ({ error: { detail: 'down' } }))),
      saveProfile: vi.fn(),
    };
    snackSpy = { open: vi.fn() };
    await TestBed.configureTestingModule({
      imports: [JobProfileFormComponent],
      providers: [
        provideNoopAnimations(),
        provideRouter([]),
        { provide: JobMatchingApiService, useValue: apiSpy },
        { provide: MatSnackBar, useValue: snackSpy },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(JobProfileFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    expect(component.error).toBe('down');
  });

  it('adds and removes chips, ignoring duplicates and blanks', async () => {
    await setup();
    const clear = vi.fn();
    component.addChip('keywords', { value: ' infrastructure ', chipInput: { clear } } as never);
    expect(component.chips.keywords).toEqual(['platform', 'infrastructure']);
    expect(clear).toHaveBeenCalled();

    component.addChip('keywords', { value: 'infrastructure', chipInput: { clear } } as never);
    expect(component.chips.keywords).toEqual(['platform', 'infrastructure']);

    component.addChip('keywords', { value: '  ', chipInput: { clear } } as never);
    expect(component.chips.keywords).toEqual(['platform', 'infrastructure']);

    component.removeChip('keywords', 'platform');
    expect(component.chips.keywords).toEqual(['infrastructure']);
  });

  it('assembles the snake_case payload from chips and controls', async () => {
    await setup();
    component.removeChip('target_titles', 'Staff Engineer');
    const clear = vi.fn();
    component.addChip('target_titles', { value: 'Platform Eng', chipInput: { clear } } as never);
    component.form.patchValue({ salary_min: 200000, title_fit: 0.5 });

    const payload = component.toProfile();
    expect(payload.target_titles).toEqual(['Platform Eng']);
    expect(payload.salary_min).toBe(200000);
    expect(payload.weights.title_fit).toBe(0.5);
    expect(payload.weights.skills_fit).toBe(0.2);
    expect(payload.excluded_companies).toEqual(['Bad Co']);
  });

  it('saves and confirms via snackbar', async () => {
    await setup();
    apiSpy.saveProfile.mockReturnValue(of(makeProfile()));
    component.save();
    expect(apiSpy.saveProfile).toHaveBeenCalledWith(component.toProfile());
    expect(component.saving).toBe(false);
    expect(component.savedAt).not.toBeNull();
    expect(snackSpy.open).toHaveBeenCalledWith(
      'Career profile saved to your user profile.',
      'Dismiss',
      expect.anything()
    );
  });

  it('surfaces the backend detail when saving fails', async () => {
    await setup();
    apiSpy.saveProfile.mockReturnValue(
      throwError(() => ({ error: { detail: 'Career profile storage requires Postgres' } }))
    );
    component.save();
    expect(component.saving).toBe(false);
    expect(snackSpy.open).toHaveBeenCalledWith(
      'Career profile storage requires Postgres',
      'Dismiss',
      expect.anything()
    );
  });

  it('does not save while invalid or already saving', async () => {
    await setup();
    component.form.patchValue({ salary_min: -5 });
    component.save();
    expect(apiSpy.saveProfile).not.toHaveBeenCalled();

    component.form.patchValue({ salary_min: 0 });
    component.saving = true;
    component.save();
    expect(apiSpy.saveProfile).not.toHaveBeenCalled();
  });

  it('computes each dimension\'s share of the final score from normalized weights', async () => {
    await setup();
    // Weights from makeProfile: 0.3/0.1/0.15/0.15/0.1/0.2 → total 1.0.
    expect(component.weightShare('title_fit')).toBe(30);
    expect(component.weightShare('skills_fit')).toBe(20);
    // All-zero weights fall back to a uniform split (mirrors the ranker).
    component.form.patchValue({
      title_fit: 0,
      seniority_fit: 0,
      location_fit: 0,
      comp_fit: 0,
      company_fit: 0,
      skills_fit: 0,
    });
    expect(component.weightShare('comp_fit')).toBe(17);
  });

  it('tracks dirty state across edits and save', async () => {
    await setup();
    expect(component.dirty).toBe(false);

    const clear = vi.fn();
    component.addChip('keywords', { value: 'infra', chipInput: { clear } } as never);
    expect(component.dirty).toBe(true);

    apiSpy.saveProfile.mockReturnValue(of(makeProfile()));
    component.save();
    expect(component.dirty).toBe(false);

    component.form.controls.salary_min.markAsDirty();
    expect(component.dirty).toBe(true);
  });
});
