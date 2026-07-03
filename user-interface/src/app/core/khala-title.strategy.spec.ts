import { TestBed } from '@angular/core/testing';
import { Title } from '@angular/platform-browser';
import { RouterStateSnapshot } from '@angular/router';
import { vi } from 'vitest';
import { KhalaTitleStrategy } from './khala-title.strategy';

/** Build a minimal RouterStateSnapshot-shaped tree with per-level data. */
function snapshotWith(...datas: Record<string, unknown>[]): RouterStateSnapshot {
  let child: unknown = null;
  for (let i = datas.length - 1; i >= 0; i--) {
    child = { data: datas[i], firstChild: child };
  }
  return { root: { data: {}, firstChild: child } } as unknown as RouterStateSnapshot;
}

describe('KhalaTitleStrategy', () => {
  let strategy: KhalaTitleStrategy;
  let setTitle: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    setTitle = vi.fn();
    TestBed.configureTestingModule({
      providers: [{ provide: Title, useValue: { setTitle } }],
    });
    strategy = TestBed.inject(KhalaTitleStrategy);
  });

  it('sets "<title> | Khala" from the deepest route data', () => {
    strategy.updateTitle(snapshotWith({}, { title: 'Job Matching' }));
    expect(setTitle).toHaveBeenCalledWith('Job Matching | Khala');
  });

  it('keeps the nearest ancestor title when the leaf has none', () => {
    strategy.updateTitle(snapshotWith({ title: 'Parent' }, {}));
    expect(setTitle).toHaveBeenCalledWith('Parent | Khala');
  });

  it('falls back to the app name when no route declares a title', () => {
    strategy.updateTitle(snapshotWith({}, {}));
    expect(setTitle).toHaveBeenCalledWith('Khala');
  });
});
