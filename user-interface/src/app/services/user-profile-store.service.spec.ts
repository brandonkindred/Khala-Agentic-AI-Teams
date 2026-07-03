import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { UserProfileApiService } from './user-profile-api.service';
import { UserProfileStore } from './user-profile-store.service';

const PROFILE = {
  user_id: 'default',
  display_name: 'Ada Lovelace',
  email: '',
  bio: '',
  preferences: { avatar_color: 'green' },
  created_at: '',
  updated_at: '',
};

describe('UserProfileStore', () => {
  let api: { getProfile: ReturnType<typeof vi.fn> };
  let store: UserProfileStore;

  beforeEach(() => {
    api = { getProfile: vi.fn().mockReturnValue(of(PROFILE)) };
    TestBed.configureTestingModule({
      providers: [{ provide: UserProfileApiService, useValue: api }],
    });
    store = TestBed.inject(UserProfileStore);
  });

  it('starts with no identity and the default color', () => {
    expect(store.displayName()).toBe('');
    expect(store.hasIdentity()).toBe(false);
    expect(store.avatarColorKey()).toBe('amber');
  });

  it('refresh() pulls name and normalized color from the API', () => {
    store.refresh();
    expect(store.displayName()).toBe('Ada Lovelace');
    expect(store.hasIdentity()).toBe(true);
    expect(store.avatarColorKey()).toBe('green');
  });

  it('refresh() keeps last-known identity on error', () => {
    store.set('Prior', 'blue');
    api.getProfile.mockReturnValue(throwError(() => new Error('boom')));
    store.refresh();
    expect(store.displayName()).toBe('Prior');
    expect(store.avatarColorKey()).toBe('blue');
  });

  it('refresh() fetches silently (suppressing the global error toast)', () => {
    store.refresh();
    expect(api.getProfile).toHaveBeenCalledWith({ silent: true });
  });

  it('a set() during an in-flight refresh() wins over the stale response', async () => {
    const { Subject } = await import('rxjs');
    const pending = new Subject<typeof PROFILE>();
    api.getProfile.mockReturnValue(pending);
    store.refresh(); // captures writeSeq, request in flight
    store.set('Fresh Save', 'red'); // user saves before the fetch resolves
    pending.next(PROFILE); // stale boot fetch resolves last
    pending.complete();
    // The stale fetch must NOT overwrite the fresh save.
    expect(store.displayName()).toBe('Fresh Save');
    expect(store.avatarColorKey()).toBe('red');
  });

  it('set() normalizes an unknown color to the default and blank name', () => {
    store.set('', 'magenta');
    expect(store.hasIdentity()).toBe(false);
    expect(store.avatarColorKey()).toBe('amber');
  });
});
