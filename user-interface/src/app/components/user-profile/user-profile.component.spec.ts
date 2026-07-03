import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { provideRouter } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { vi } from 'vitest';
import { UserProfileApiService } from '../../services/user-profile-api.service';
import { UserProfileComponent } from './user-profile.component';

const PROFILE = {
  user_id: 'default',
  display_name: 'Brandon',
  email: 'b@example.com',
  bio: 'builder',
  preferences: {},
  created_at: '',
  updated_at: '',
};

const ASSOCIATIONS = [
  { id: 'a1', user_id: 'default', artifact_type: 'brand', team: 'branding', artifact_id: 'brand_1', label: 'Acme', role: 'owner', created_at: '' },
  { id: 'a2', user_id: 'default', artifact_type: 'project', team: 'coding_team', artifact_id: 'job_2', label: 'Repo', role: 'owner', created_at: '' },
];

const INTEGRATIONS = [{ id: 'slack', type: 'slack', enabled: true, channel: '#eng' }];

const OVERVIEW = { profile: PROFILE, associations: ASSOCIATIONS, integrations: INTEGRATIONS };

describe('UserProfileComponent', () => {
  let component: UserProfileComponent;
  let fixture: ComponentFixture<UserProfileComponent>;
  let apiSpy: {
    getOverview: ReturnType<typeof vi.fn>;
    updateProfile: ReturnType<typeof vi.fn>;
  };

  async function setup(): Promise<void> {
    await TestBed.configureTestingModule({
      imports: [UserProfileComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideRouter([]),
        { provide: UserProfileApiService, useValue: apiSpy },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(UserProfileComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  }

  beforeEach(() => {
    apiSpy = {
      getOverview: vi.fn().mockReturnValue(of(OVERVIEW)),
      updateProfile: vi.fn().mockReturnValue(of(PROFILE)),
    };
  });

  afterEach(() => {
    fixture?.destroy();
  });

  it('should create and load the profile in a single request', async () => {
    await setup();
    expect(component).toBeTruthy();
    expect(apiSpy.getOverview).toHaveBeenCalledTimes(1);
    expect(component.form.value.display_name).toBe('Brandon');
    // The spinner must clear once the single request resolves.
    expect(component.loading).toBe(false);
  });

  it('should group associations by type and count them', async () => {
    await setup();
    expect(component.groups.length).toBe(2);
    expect(component.totalAssociations).toBe(2);
    expect(component.groups[0].type).toBe('brand');
    // Each group must carry the icon + label the template renders.
    expect(component.groups[0].icon).toBeTruthy();
    expect(component.groups[0].label).toBeTruthy();
  });

  it('should reload the overview when the refresh control is clicked', async () => {
    await setup();
    expect(apiSpy.getOverview).toHaveBeenCalledTimes(1);
    const btn = (fixture.nativeElement as HTMLElement).querySelector('.up-refresh') as HTMLButtonElement;
    expect(btn).toBeTruthy();
    btn.click();
    expect(apiSpy.getOverview).toHaveBeenCalledTimes(2);
  });

  it('should load integration status', async () => {
    await setup();
    expect(component.integrations).toEqual(INTEGRATIONS);
  });

  it('should clear stale data and set an error when a re-load returns a malformed response', async () => {
    await setup();
    // First load populated groups/integrations.
    expect(component.groups.length).toBe(2);
    expect(component.integrations.length).toBe(1);
    // A subsequent malformed 2xx response must clear the stale data, not show it.
    apiSpy.getOverview.mockReturnValue(of({ associations: [], integrations: [] }));
    component.load();
    fixture.detectChanges();
    expect(component.error).toBeTruthy();
    expect(component.groups).toEqual([]);
    expect(component.integrations).toEqual([]);
    expect(component.totalAssociations).toBe(0);
    expect(component.loading).toBe(false);
  });

  it('should render the empty integrations message when none are reported', async () => {
    apiSpy.getOverview.mockReturnValue(of({ ...OVERVIEW, integrations: [] }));
    await setup();
    expect(component.integrations).toEqual([]);
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('No integrations reported.');
  });

  it('should fall back to artifact_id when an association has no label', async () => {
    apiSpy.getOverview.mockReturnValue(
      of({
        ...OVERVIEW,
        associations: [{ ...ASSOCIATIONS[0], label: '', artifact_id: 'brand_xyz' }],
      }),
    );
    await setup();
    const labels = (fixture.nativeElement as HTMLElement).querySelectorAll('.up-item-label');
    const text = Array.from(labels)
      .map((el) => el.textContent?.trim())
      .join(' ');
    expect(text).toContain('brand_xyz');
  });

  it('should omit the channel meta when an integration has no channel', async () => {
    apiSpy.getOverview.mockReturnValue(
      of({ ...OVERVIEW, integrations: [{ id: 'github', type: 'github', enabled: true, channel: null }] }),
    );
    await setup();
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('github');
    expect(text).not.toContain('#eng');
  });

  it('should set an error when loading fails', async () => {
    apiSpy.getOverview.mockReturnValue(throwError(() => new Error('boom')));
    await setup();
    expect(component.error).toBeTruthy();
    expect(component.loading).toBe(false);
  });

  it('should clear a stale success banner on reload', async () => {
    await setup();
    component.success = 'Profile saved.';
    component.load();
    expect(component.success).toBeNull();
  });

  it('should save valid profile edits', async () => {
    await setup();
    component.form.patchValue({ display_name: 'New Name' });
    component.form.markAsDirty(); // simulate a user edit dirtying the form
    expect(component.form.dirty).toBe(true);
    component.save();
    expect(apiSpy.updateProfile).toHaveBeenCalledWith(
      expect.objectContaining({ display_name: 'New Name' }),
    );
    expect(component.success).toBe('Profile saved.');
    // The form matches the persisted state after a successful save.
    expect(component.form.pristine).toBe(true);
  });

  it('should not save when the email is invalid', async () => {
    await setup();
    component.form.patchValue({ email: 'not-an-email' });
    component.save();
    expect(apiSpy.updateProfile).not.toHaveBeenCalled();
    // The documented side effect: the form is marked touched so validators show.
    expect(component.form.touched).toBe(true);
  });

  it('should keep previously loaded data when a reload fails with an HTTP error', async () => {
    await setup();
    // First load populated groups/integrations.
    expect(component.groups.length).toBe(2);
    expect(component.integrations.length).toBe(1);
    // A subsequent HTTP error must set the error but leave the last-good view.
    apiSpy.getOverview.mockReturnValue(throwError(() => new Error('boom')));
    component.load();
    expect(component.error).toBeTruthy();
    expect(component.loading).toBe(false);
    expect(component.groups.length).toBe(2);
    expect(component.integrations.length).toBe(1);
    expect(component.totalAssociations).toBe(2);
  });

  it('should treat a non-array associations/integrations field as malformed', async () => {
    // A 2xx body where associations isn't an array must not throw in grouping.
    apiSpy.getOverview.mockReturnValue(
      of({ profile: PROFILE, associations: {}, integrations: [] }),
    );
    await setup();
    expect(component.error).toBeTruthy();
    expect(component.groups).toEqual([]);
  });

  it('should treat a non-array integrations field as malformed', async () => {
    // The guard checks integrations too — a non-array must be rejected, not iterated.
    apiSpy.getOverview.mockReturnValue(
      of({ profile: PROFILE, associations: [], integrations: {} }),
    );
    await setup();
    expect(component.error).toBeTruthy();
    expect(component.integrations).toEqual([]);
  });

  it('should not start a second save while one is already in flight', async () => {
    await setup();
    component.saving = true;
    component.form.patchValue({ display_name: 'New Name' });
    component.save();
    expect(apiSpy.updateProfile).not.toHaveBeenCalled();
  });

  it('should not start a second load while one is already in flight', async () => {
    await setup();
    expect(apiSpy.getOverview).toHaveBeenCalledTimes(1);
    component.loading = true;
    component.load();
    expect(apiSpy.getOverview).toHaveBeenCalledTimes(1);
  });

  it('should surface a save error', async () => {
    apiSpy.updateProfile.mockReturnValue(throwError(() => new Error('boom')));
    await setup();
    component.save();
    expect(component.error).toBeTruthy();
    expect(component.saving).toBe(false);
  });

  it('should set an error when the overview response is malformed', async () => {
    // A 2xx response missing `profile` must not throw deep in render.
    apiSpy.getOverview.mockReturnValue(of({ associations: [], integrations: [] }));
    await setup();
    expect(component.error).toBeTruthy();
    expect(component.loading).toBe(false);
    expect(component.groups).toEqual([]);
  });

  it('should show no groups when there are no associations', async () => {
    apiSpy.getOverview.mockReturnValue(of({ ...OVERVIEW, associations: [] }));
    await setup();
    expect(component.totalAssociations).toBe(0);
    expect(component.groups).toEqual([]);
  });

  it('should load a stored avatar color into the form', async () => {
    apiSpy.getOverview.mockReturnValue(
      of({ ...OVERVIEW, profile: { ...PROFILE, preferences: { theme: 'dark', avatar_color: 'blue' } } }),
    );
    await setup();
    expect(component.form.value.avatar_color).toBe('blue');
  });

  it('should default the avatar color when preferences are missing or garbage', async () => {
    for (const preferences of [null, 'nonsense', ['x'], { avatar_color: 42 }, { avatar_color: 'magenta' }]) {
      apiSpy.getOverview.mockReturnValue(of({ ...OVERVIEW, profile: { ...PROFILE, preferences } }));
      await setup();
      expect(component.form.value.avatar_color).toBe('amber');
      fixture.destroy();
      TestBed.resetTestingModule();
    }
  });

  it('should mark the form dirty and check the matching swatch when a color is selected', async () => {
    await setup();
    expect(component.form.dirty).toBe(false);
    component.selectAvatarColor('green');
    fixture.detectChanges();
    expect(component.form.value.avatar_color).toBe('green');
    expect(component.form.dirty).toBe(true);
    const checked = (fixture.nativeElement as HTMLElement).querySelector(
      '.up-swatch[aria-checked="true"]',
    ) as HTMLButtonElement;
    expect(checked).toBeTruthy();
    expect(checked.getAttribute('aria-label')).toBe('Green');
  });

  it('should merge the avatar color into existing preferences on save (no clobbering)', async () => {
    // Regression guard: the backend PUT replaces preferences wholesale, so a
    // save must carry the previously loaded keys alongside avatar_color.
    apiSpy.getOverview.mockReturnValue(
      of({ ...OVERVIEW, profile: { ...PROFILE, preferences: { theme: 'dark' } } }),
    );
    await setup();
    component.selectAvatarColor('green');
    component.save();
    expect(apiSpy.updateProfile).toHaveBeenCalledWith(
      expect.objectContaining({ preferences: { theme: 'dark', avatar_color: 'green' } }),
    );
  });

  it('should keep merging against saved preferences on back-to-back saves', async () => {
    apiSpy.getOverview.mockReturnValue(
      of({ ...OVERVIEW, profile: { ...PROFILE, preferences: { theme: 'dark' } } }),
    );
    await setup();
    component.selectAvatarColor('green');
    component.save();
    // A second save without a reload must still carry the unrelated key.
    component.selectAvatarColor('red');
    component.save();
    expect(apiSpy.updateProfile).toHaveBeenLastCalledWith(
      expect.objectContaining({ preferences: { theme: 'dark', avatar_color: 'red' } }),
    );
  });

  it('should block saving until a load has succeeded', async () => {
    // A save after a failed load would PUT constructor defaults + empty
    // preferences, which the backend applies wholesale — wiping the profile.
    apiSpy.getOverview.mockReturnValue(throwError(() => new Error('boom')));
    await setup();
    expect(component.profileLoaded).toBe(false);
    const btn = (fixture.nativeElement as HTMLElement).querySelector(
      'button[type="submit"]',
    ) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    component.save();
    expect(apiSpy.updateProfile).not.toHaveBeenCalled();
  });

  it('should re-enable saving once a retry load succeeds', async () => {
    apiSpy.getOverview.mockReturnValue(throwError(() => new Error('boom')));
    await setup();
    apiSpy.getOverview.mockReturnValue(of(OVERVIEW));
    component.load();
    expect(component.profileLoaded).toBe(true);
    component.save();
    expect(apiSpy.updateProfile).toHaveBeenCalledTimes(1);
  });

  it('should rove tabindex so only the checked swatch is a tab stop', async () => {
    await setup();
    const radios = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll<HTMLButtonElement>('.up-swatch'),
    );
    expect(radios.length).toBe(4);
    expect(radios.filter((r) => r.tabIndex === 0).length).toBe(1);
    expect(radios.filter((r) => r.tabIndex === -1).length).toBe(3);
    const checked = radios.find((r) => r.tabIndex === 0);
    expect(checked?.getAttribute('aria-checked')).toBe('true');
  });

  it('should move the selection with arrow keys, wrapping at the ends', async () => {
    await setup();
    expect(component.form.value.avatar_color).toBe('amber');
    // Roving tabindex: each keydown targets the currently checked radio.
    const checkedRadio = () =>
      (fixture.nativeElement as HTMLElement).querySelector(
        '.up-swatch[aria-checked="true"]',
      ) as HTMLButtonElement;
    const press = (key: string) => {
      checkedRadio().dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
      fixture.detectChanges();
    };
    press('ArrowRight');
    expect(component.form.value.avatar_color).toBe('green');
    press('ArrowLeft');
    expect(component.form.value.avatar_color).toBe('amber');
    // Left from the first entry wraps to the last.
    press('ArrowUp');
    expect(component.form.value.avatar_color).toBe('red');
    press('ArrowDown');
    expect(component.form.value.avatar_color).toBe('amber');
    expect(component.form.dirty).toBe(true);
  });

  it('should leave non-arrow keys alone in the swatch radiogroup', async () => {
    await setup();
    const radio = (fixture.nativeElement as HTMLElement).querySelector('.up-swatch') as HTMLButtonElement;
    const event = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
    radio.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    expect(component.form.value.avatar_color).toBe('amber');
  });

  it('should live-update the avatar initials from the display name control', async () => {
    await setup();
    component.form.patchValue({ display_name: 'Grace Hopper' });
    fixture.detectChanges();
    const circle = (fixture.nativeElement as HTMLElement).querySelector('.ia-circle');
    expect(circle?.textContent?.trim()).toBe('GH');
  });
});
