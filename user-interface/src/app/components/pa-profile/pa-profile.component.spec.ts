import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { SimpleChange } from '@angular/core';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import { PaProfileComponent } from './pa-profile.component';

describe('PaProfileComponent', () => {
  let component: PaProfileComponent;
  let fixture: ComponentFixture<PaProfileComponent>;
  let apiSpy: {
    getProfile: ReturnType<typeof vi.fn>;
    updateProfile: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    apiSpy = {
      getProfile: vi.fn().mockReturnValue(
        of({
          identity: { full_name: 'A', preferred_name: 'a', email: 'a@b.com', timezone: 'UTC' },
          preferences: {
            food_likes: ['pizza'],
            food_dislikes: [],
            cuisines_ranked: ['italian'],
            dietary_restrictions: [],
          },
          goals: { short_term_goals: ['x'], long_term_goals: ['y'], dreams: ['z'] },
          professional: { job_title: 'Eng', company: 'C', industry: 'Tech', work_schedule: 'M-F' },
        }),
      ),
      updateProfile: vi.fn().mockReturnValue(of({})),
    };
    await TestBed.configureTestingModule({
      imports: [PaProfileComponent, NoopAnimationsModule],
      providers: [{ provide: PersonalAssistantApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(PaProfileComponent);
    component = fixture.componentInstance;
    component.userId = 'u1';
    fixture.detectChanges();
  });

  it('creates and loads profile', () => {
    expect(component).toBeTruthy();
    expect(apiSpy.getProfile).toHaveBeenCalledWith('u1');
    expect(component.form.get('fullName')?.value).toBe('A');
    expect(component.form.get('foodLikes')?.value).toBe('pizza');
  });

  it('loadProfile handles error', () => {
    apiSpy.getProfile.mockReturnValue(throwError(() => ({})));
    (component as unknown as { loadProfile: () => void }).loadProfile();
    expect(component.loading).toBe(false);
  });

  it('ngOnChanges reloads on userId change', () => {
    apiSpy.getProfile.mockClear();
    component.ngOnChanges({ userId: new SimpleChange('u1', 'u2', false) });
    expect(apiSpy.getProfile).toHaveBeenCalled();
  });

  it('ngOnChanges ignores first change', () => {
    apiSpy.getProfile.mockClear();
    component.ngOnChanges({ userId: new SimpleChange(undefined, 'u1', true) });
    expect(apiSpy.getProfile).not.toHaveBeenCalled();
  });

  it('onUserIdBlur emits change and reloads when different', () => {
    const spy = vi.fn();
    component.userIdChange.subscribe(spy);
    component.form.patchValue({ userId: 'u2' });
    apiSpy.getProfile.mockClear();
    component.onUserIdBlur();
    expect(spy).toHaveBeenCalledWith('u2');
    expect(component.userId).toBe('u2');
    expect(apiSpy.getProfile).toHaveBeenCalledWith('u2');
  });

  it('onUserIdBlur ignores no change', () => {
    component.form.patchValue({ userId: 'u1' });
    apiSpy.getProfile.mockClear();
    component.onUserIdBlur();
    expect(apiSpy.getProfile).not.toHaveBeenCalled();
  });

  it('onUserIdBlur ignores empty', () => {
    component.form.patchValue({ userId: '   ' });
    apiSpy.getProfile.mockClear();
    component.onUserIdBlur();
    expect(apiSpy.getProfile).not.toHaveBeenCalled();
  });

  it('onSave dispatches four updates and clears loading at end', () => {
    apiSpy.updateProfile.mockClear();
    component.onSave();
    expect(apiSpy.updateProfile).toHaveBeenCalledTimes(4);
    expect(component.loading).toBe(false);
  });

  it('onSave handles error path on some updates', () => {
    apiSpy.updateProfile = vi
      .fn()
      .mockReturnValueOnce(of({}))
      .mockReturnValueOnce(throwError(() => ({})))
      .mockReturnValueOnce(of({}))
      .mockReturnValueOnce(of({}));
    component.onSave();
    expect(component.loading).toBe(false);
  });
});
