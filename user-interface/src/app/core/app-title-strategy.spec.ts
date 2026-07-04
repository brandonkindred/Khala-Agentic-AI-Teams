import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Title } from '@angular/platform-browser';
import { TitleStrategy, provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { vi } from 'vitest';
import { AppTitleStrategy } from './app-title-strategy';

@Component({ standalone: true, template: '' })
class BlankComponent {}

describe('AppTitleStrategy', () => {
  let title: { setTitle: ReturnType<typeof vi.fn> };

  beforeEach(() => {
    title = { setTitle: vi.fn() };
    TestBed.configureTestingModule({
      providers: [
        { provide: Title, useValue: title },
        { provide: TitleStrategy, useClass: AppTitleStrategy },
        provideRouter([
          { path: 'profile', component: BlankComponent, title: 'User Profile' },
          { path: 'bare', component: BlankComponent },
          {
            path: 'parent',
            component: BlankComponent,
            title: 'Parent',
            children: [{ path: 'child', component: BlankComponent, title: 'Child' }],
          },
          {
            path: 'keep',
            component: BlankComponent,
            title: 'Kept',
            children: [{ path: 'leaf', component: BlankComponent }],
          },
        ]),
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  it('sets the route title with the app-name suffix', async () => {
    await RouterTestingHarness.create('/profile');
    expect(title.setTitle).toHaveBeenCalledWith('User Profile | Khala');
  });

  it('falls back to the bare app name when the route declares no title', async () => {
    await RouterTestingHarness.create('/bare');
    expect(title.setTitle).toHaveBeenCalledWith('Khala');
  });

  it('lets a child title override its parent', async () => {
    await RouterTestingHarness.create('/parent/child');
    expect(title.setTitle).toHaveBeenCalledWith('Child | Khala');
  });

  it('keeps the deepest declared title when a leaf declares none', async () => {
    await RouterTestingHarness.create('/keep/leaf');
    expect(title.setTitle).toHaveBeenCalledWith('Kept | Khala');
  });
});
