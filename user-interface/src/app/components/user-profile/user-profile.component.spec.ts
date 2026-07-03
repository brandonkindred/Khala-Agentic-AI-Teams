import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { provideRouter } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { MatSnackBar } from '@angular/material/snack-bar';
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
    getProfile: ReturnType<typeof vi.fn>;
  };
  let snackBar: { open: ReturnType<typeof vi.fn> };

  async function setup(): Promise<void> {
    await TestBed.configureTestingModule({
      imports: [UserProfileComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideRouter([]),
        { provide: UserProfileApiService, useValue: apiSpy },
        { provide: MatSnackBar, useValue: snackBar },
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
      getProfile: vi.fn().mockReturnValue(of(PROFILE)),
    };
    snackBar = { open: vi.fn() };
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

  it('should render the empty integrations state when none are reported', async () => {
    apiSpy.getOverview.mockReturnValue(of({ ...OVERVIEW, integrations: [] }));
    await setup();
    expect(component.integrations).toEqual([]);
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('No integrations connected');
    // Uses the shared empty-state component, not an inline paragraph.
    expect((fixture.nativeElement as HTMLElement).querySelector('app-empty-state')).toBeTruthy();
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

  it('should save valid profile edits and confirm via a snackbar', async () => {
    await setup();
    component.form.patchValue({ display_name: 'New Name' });
    component.form.markAsDirty(); // simulate a user edit dirtying the form
    expect(component.form.dirty).toBe(true);
    component.save();
    expect(apiSpy.updateProfile).toHaveBeenCalledWith(
      expect.objectContaining({ display_name: 'New Name' }),
    );
    // Transient confirmation, not a persistent banner.
    expect(snackBar.open).toHaveBeenCalledWith('Profile saved.', 'Dismiss', { duration: 3000 });
    // The form matches the persisted state after a successful save.
    expect(component.form.pristine).toBe(true);
  });

  it('should report unsaved changes while the form is dirty, clearing after a successful save', async () => {
    await setup();
    expect(component.hasUnsavedChanges()).toBe(false);
    component.form.patchValue({ bio: 'edit' });
    component.form.markAsDirty();
    expect(component.hasUnsavedChanges()).toBe(true);
    component.save(); // success path marks pristine
    expect(component.hasUnsavedChanges()).toBe(false);
  });

  it('should still report unsaved changes DURING an in-flight save', async () => {
    // Navigating away mid-save cancels the request (takeUntilDestroyed), so the
    // guard must keep prompting until the save actually completes.
    const { Subject } = await import('rxjs');
    const pending = new Subject<typeof PROFILE>();
    apiSpy.updateProfile.mockReturnValue(pending);
    await setup();
    component.form.patchValue({ bio: 'edit' });
    component.form.markAsDirty();
    component.save();
    expect(component.saving).toBe(true);
    expect(component.hasUnsavedChanges()).toBe(true); // still dirty, save not yet landed
    pending.next(PROFILE);
    pending.complete();
    expect(component.saving).toBe(false);
    expect(component.hasUnsavedChanges()).toBe(false);
  });

  it('should prompt the browser on unload while there are unsaved changes', async () => {
    await setup();
    component.form.patchValue({ bio: 'edit' });
    component.form.markAsDirty();
    const event = { preventDefault: vi.fn(), returnValue: undefined } as unknown as BeforeUnloadEvent;
    component.onBeforeUnload(event);
    expect(event.preventDefault).toHaveBeenCalled();
    // A pristine form leaves the event untouched.
    component.form.markAsPristine();
    const clean = { preventDefault: vi.fn(), returnValue: undefined } as unknown as BeforeUnloadEvent;
    component.onBeforeUnload(clean);
    expect(clean.preventDefault).not.toHaveBeenCalled();
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
    // Two component-level cases cover the two load paths: a null container
    // (the optional chain yields undefined) and an unknown stored key
    // (resolveAvatarColor falls back). Value-level garbage shapes are
    // unit-tested on resolveAvatarColor in the avatar spec.
    for (const preferences of [null, { avatar_color: 'magenta' }]) {
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

  it('should send only the avatar_color preference key on save (server merges)', async () => {
    // Clobber-prevention regression: unrelated preference keys survive because
    // the backend merges key-by-key — the client must NOT send a snapshot of
    // other features' keys (a stale snapshot is what caused lost updates).
    apiSpy.getOverview.mockReturnValue(
      of({ ...OVERVIEW, profile: { ...PROFILE, preferences: { theme: 'dark', avatar_color: 'blue' } } }),
    );
    await setup();
    component.selectAvatarColor('green');
    component.save();
    expect(apiSpy.updateProfile).toHaveBeenCalledWith(
      expect.objectContaining({ preferences: { avatar_color: 'green' } }),
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

  it('should offer a Retry button in the error banner before the first successful load', async () => {
    apiSpy.getOverview.mockReturnValue(throwError(() => new Error('boom')));
    await setup();
    const retry = (fixture.nativeElement as HTMLElement).querySelector('.up-retry') as HTMLButtonElement;
    expect(retry).toBeTruthy();
    apiSpy.getOverview.mockReturnValue(of(OVERVIEW));
    retry.click();
    fixture.detectChanges();
    expect(component.profileLoaded).toBe(true);
    expect((fixture.nativeElement as HTMLElement).querySelector('.up-retry')).toBeNull();
  });

  it('should not offer Retry on a save error (reloading would discard unsaved edits)', async () => {
    apiSpy.updateProfile.mockReturnValue(throwError(() => new Error('boom')));
    await setup();
    component.save();
    fixture.detectChanges();
    expect(component.error).toBeTruthy();
    expect((fixture.nativeElement as HTMLElement).querySelector('.up-retry')).toBeNull();
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

  it('should omit preferences entirely on a save when no swatch was picked', async () => {
    // A bio-only save must not write avatar_color at all: it would stamp the
    // default onto never-chose profiles and could overwrite a concurrent
    // tab's newer choice with this tab's stale loaded value. The key must be
    // ABSENT — not present-with-null/undefined — so the backend merge is
    // never entered (hence toHaveProperty, not a loose objectContaining).
    apiSpy.getOverview.mockReturnValue(
      of({ ...OVERVIEW, profile: { ...PROFILE, preferences: { theme: 'dark', avatar_color: 'blue' } } }),
    );
    await setup();
    component.form.patchValue({ bio: 'updated' });
    component.save();
    expect(apiSpy.updateProfile.mock.calls[0][0]).not.toHaveProperty('preferences');
  });

  it('should stop sending the color on later saves once it is persisted', async () => {
    // After a successful save the control is pristine again; the stored color
    // survives server-side via the merge, so later saves omit the key.
    await setup(); // PROFILE.preferences is {} — nothing stored initially
    component.selectAvatarColor('green');
    component.save();
    expect(apiSpy.updateProfile.mock.calls[0][0]).toHaveProperty('preferences', {
      avatar_color: 'green',
    });
    component.save(); // pristine again after success; no swatch touched
    expect(apiSpy.updateProfile.mock.calls[1][0]).not.toHaveProperty('preferences');
  });

  it('should preserve unsaved edits when a reload happens mid-edit', async () => {
    // The "Refresh linked work" button re-runs load(); a dirty form must keep
    // the user's edits instead of being overwritten by server state.
    await setup();
    component.form.patchValue({ bio: 'work in progress' });
    component.form.markAsDirty();
    apiSpy.getOverview.mockReturnValue(
      of({ ...OVERVIEW, profile: { ...PROFILE, bio: 'server bio' } }),
    );
    component.load();
    expect(component.form.value.bio).toBe('work in progress');
    // The non-form view state still refreshes.
    expect(component.groups.length).toBe(2);
  });

  it('should patch the form from a reload when it is pristine', async () => {
    await setup();
    apiSpy.getOverview.mockReturnValue(
      of({ ...OVERVIEW, profile: { ...PROFILE, bio: 'server bio' } }),
    );
    component.load();
    expect(component.form.value.bio).toBe('server bio');
  });

  it('should move focus to the first field after a successful banner retry', async () => {
    vi.useFakeTimers();
    apiSpy.getOverview.mockReturnValue(throwError(() => new Error('boom')));
    await setup();
    apiSpy.getOverview.mockReturnValue(of(OVERVIEW));
    component.retryLoad();
    fixture.detectChanges(); // form renders now that loading finished
    vi.advanceTimersByTime(0); // flush the deferred focus
    const input = (fixture.nativeElement as HTMLElement).querySelector(
      'input[formcontrolname="display_name"]',
    );
    expect(document.activeElement).toBe(input);
    vi.useRealTimers();
  });

  it('should live-update the avatar initials from the display name control', async () => {
    await setup();
    component.form.patchValue({ display_name: 'Grace Hopper' });
    fixture.detectChanges();
    const circle = (fixture.nativeElement as HTMLElement).querySelector('.ia-circle');
    expect(circle?.textContent?.trim()).toBe('GH');
  });
});
