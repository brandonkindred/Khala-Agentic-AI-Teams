import { TestBed } from '@angular/core/testing';
import { NavStateService } from './nav-state.service';

describe('NavStateService', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    localStorage.clear();
  });

  it('starts with empty favorites when no localStorage', () => {
    const service = TestBed.runInInjectionContext(() => new NavStateService());
    expect(service.favorites().size).toBe(0);
    expect(service.favoriteItems()).toEqual([]);
    expect(service.isFavorite('blogging')).toBe(false);
  });

  it('loads favorites from localStorage', () => {
    localStorage.setItem('kh-nav-favorites', JSON.stringify(['blogging']));
    const service = TestBed.runInInjectionContext(() => new NavStateService());
    expect(service.isFavorite('blogging')).toBe(true);
    expect(service.favoriteItems().some((i) => i.id === 'blogging')).toBe(true);
  });

  it('ignores non-array data in localStorage gracefully', () => {
    localStorage.setItem('kh-nav-favorites', JSON.stringify({ not: 'array' }));
    const service = TestBed.runInInjectionContext(() => new NavStateService());
    expect(service.favorites().size).toBe(0);
  });

  it('ignores corrupted JSON', () => {
    localStorage.setItem('kh-nav-favorites', 'not-json');
    const service = TestBed.runInInjectionContext(() => new NavStateService());
    expect(service.favorites().size).toBe(0);
  });

  it('filters non-string entries', () => {
    localStorage.setItem('kh-nav-favorites', JSON.stringify(['blogging', 5, null]));
    const service = TestBed.runInInjectionContext(() => new NavStateService());
    expect([...service.favorites()]).toEqual(['blogging']);
  });

  it('toggleFavorite adds when missing and removes when present', () => {
    const service = TestBed.runInInjectionContext(() => new NavStateService());
    service.toggleFavorite('blogging');
    expect(service.isFavorite('blogging')).toBe(true);
    expect(JSON.parse(localStorage.getItem('kh-nav-favorites')!)).toContain('blogging');
    service.toggleFavorite('blogging');
    expect(service.isFavorite('blogging')).toBe(false);
  });

  it('toggleFavorite swallows localStorage errors', () => {
    const service = TestBed.runInInjectionContext(() => new NavStateService());
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = () => {
      throw new Error('full');
    };
    expect(() => service.toggleFavorite('a')).not.toThrow();
    Storage.prototype.setItem = original;
  });
});
