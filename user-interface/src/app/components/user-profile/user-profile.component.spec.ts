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

  it('should create and load the profile in a single request', async () => {
    await setup();
    expect(component).toBeTruthy();
    expect(apiSpy.getOverview).toHaveBeenCalledTimes(1);
    expect(component.form.value.display_name).toBe('Brandon');
  });

  it('should group associations by type and count them', async () => {
    await setup();
    expect(component.groups.length).toBe(2);
    expect(component.totalAssociations).toBe(2);
    expect(component.groups[0].type).toBe('brand');
  });

  it('should load integration status', async () => {
    await setup();
    expect(component.integrations).toEqual(INTEGRATIONS);
  });

  it('should set an error when loading fails', async () => {
    apiSpy.getOverview.mockReturnValue(throwError(() => new Error('boom')));
    await setup();
    expect(component.error).toBeTruthy();
    expect(component.loading).toBe(false);
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

  it('should surface a save error', async () => {
    apiSpy.updateProfile.mockReturnValue(throwError(() => new Error('boom')));
    await setup();
    component.save();
    expect(component.error).toBeTruthy();
    expect(component.saving).toBe(false);
  });

  it('should show no groups when there are no associations', async () => {
    apiSpy.getOverview.mockReturnValue(of({ ...OVERVIEW, associations: [] }));
    await setup();
    expect(component.totalAssociations).toBe(0);
    expect(component.groups).toEqual([]);
  });
});
