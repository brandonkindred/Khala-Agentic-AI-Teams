import { TestBed } from '@angular/core/testing';
import { Title } from '@angular/platform-browser';
import { RouterStateSnapshot } from '@angular/router';
import { vi } from 'vitest';
import { AppTitleStrategy } from './app-title-strategy';

/** Build a fake nested router snapshot from a chain of `data.title` values. */
function snapshotFor(titles: (string | undefined)[]): RouterStateSnapshot {
  let child: unknown = null;
  for (let i = titles.length - 1; i >= 0; i--) {
    child = { data: titles[i] === undefined ? {} : { title: titles[i] }, firstChild: child };
  }
  return { root: child } as RouterStateSnapshot;
}

describe('AppTitleStrategy', () => {
  let title: { setTitle: ReturnType<typeof vi.fn> };
  let strategy: AppTitleStrategy;

  beforeEach(() => {
    title = { setTitle: vi.fn() };
    TestBed.configureTestingModule({
      providers: [AppTitleStrategy, { provide: Title, useValue: title }],
    });
    strategy = TestBed.inject(AppTitleStrategy);
  });

  it('sets the deepest route title with the app-name suffix', () => {
    strategy.updateTitle(snapshotFor(['Shell', 'User Profile']));
    expect(title.setTitle).toHaveBeenCalledWith('User Profile | Khala');
  });

  it('falls back to the bare app name when no route declares a title', () => {
    strategy.updateTitle(snapshotFor([undefined, undefined]));
    expect(title.setTitle).toHaveBeenCalledWith('Khala');
  });

  it('lets a child title override its parent', () => {
    strategy.updateTitle(snapshotFor(['Parent', undefined, 'Child']));
    expect(title.setTitle).toHaveBeenCalledWith('Child | Khala');
  });
});
