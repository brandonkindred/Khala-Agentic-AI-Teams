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
    component.save();
    expect(apiSpy.updateProfile).toHaveBeenCalledWith(
      expect.objectContaining({ display_name: 'New Name' }),
    );
    expect(component.success).toBe('Profile saved.');
  });

  it('should not save when the email is invalid', async () => {
    await setup();
    component.form.patchValue({ email: 'not-an-email' });
    component.save();
    expect(apiSpy.updateProfile).not.toHaveBeenCalled();
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
});
