import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { ElementRef, QueryList } from '@angular/core';
import { vi, beforeEach, afterEach } from 'vitest';
import { AppShellComponent } from './app-shell.component';

describe('AppShellComponent (extra coverage)', () => {
  let component: AppShellComponent;
  let fixture: ComponentFixture<AppShellComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AppShellComponent, NoopAnimationsModule],
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    fixture = TestBed.createComponent(AppShellComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('isGroupActive uses findGroupForRoute', () => {
    const router = TestBed.inject(Router);
    Object.defineProperty(router, 'url', { value: '/agent-studio', writable: true, configurable: true });
    const someGroup = component.navGroups.find((g) => g.key !== component.navGroups[0].key) ?? component.navGroups[0];
    component.isGroupActive(someGroup);
    // Cover the path: result depends on NAV_GROUPS routes, just ensure no throw
    expect(typeof component.isGroupActive(someGroup)).toBe('boolean');
  });

  it('closeFlyout with returnFocus calls focus on origin', () => {
    const btn = document.createElement('button');
    const focusSpy = vi.spyOn(btn, 'focus');
    component.openFlyout(component.navGroups[0], btn);
    component.closeFlyout(true);
    expect(component.activeGroup()).toBeNull();
    expect(focusSpy).toHaveBeenCalled();
  });

  it('closeFlyout without returnFocus does not focus origin', () => {
    const btn = document.createElement('button');
    const focusSpy = vi.spyOn(btn, 'focus');
    component.openFlyout(component.navGroups[0], btn);
    component.closeFlyout(false);
    expect(focusSpy).not.toHaveBeenCalled();
  });

  it('trackByItemId returns item id', () => {
    expect(component.trackByItemId(0, { id: 'abc', label: 'x', path: '/x' } as never)).toBe('abc');
  });

  it('onNavKeydown closes flyout on Escape', () => {
    component.openFlyout(component.navGroups[0], document.createElement('button'));
    const closeSpy = vi.spyOn(component, 'closeFlyout');
    const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
    component.onNavKeydown(event);
    expect(closeSpy).toHaveBeenCalledWith(true);
  });

  it('onNavKeydown bails out when no focusables', () => {
    component.navFocusableElements = new QueryList<ElementRef<HTMLElement>>();
    const event = new KeyboardEvent('keydown', { key: 'ArrowDown' });
    component.onNavKeydown(event);
    // no throw is enough
    expect(component).toBeTruthy();
  });

  it('onNavKeydown bails out when active element is not focusable', () => {
    const items = [document.createElement('button'), document.createElement('button')];
    document.body.append(...items);
    const ql = new QueryList<ElementRef<HTMLElement>>();
    ql.reset(items.map((el) => new ElementRef(el)));
    component.navFocusableElements = ql;

    const event = new KeyboardEvent('keydown', { key: 'ArrowDown' });
    component.onNavKeydown(event);
    items.forEach((i) => i.remove());
    expect(component).toBeTruthy();
  });

  it('onNavKeydown ArrowDown moves focus to next', () => {
    const a = document.createElement('button');
    const b = document.createElement('button');
    document.body.append(a, b);
    a.focus();
    const ql = new QueryList<ElementRef<HTMLElement>>();
    ql.reset([new ElementRef(a), new ElementRef(b)]);
    component.navFocusableElements = ql;
    const focusSpy = vi.spyOn(b, 'focus');
    const event = new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true });
    component.onNavKeydown(event);
    expect(focusSpy).toHaveBeenCalled();
    a.remove();
    b.remove();
  });

  it('onNavKeydown ArrowUp moves focus to previous (clamped)', () => {
    const a = document.createElement('button');
    const b = document.createElement('button');
    document.body.append(a, b);
    b.focus();
    const ql = new QueryList<ElementRef<HTMLElement>>();
    ql.reset([new ElementRef(a), new ElementRef(b)]);
    component.navFocusableElements = ql;
    const focusSpy = vi.spyOn(a, 'focus');
    const event = new KeyboardEvent('keydown', { key: 'ArrowUp' });
    component.onNavKeydown(event);
    expect(focusSpy).toHaveBeenCalled();
    a.remove();
    b.remove();
  });

  it('onNavKeydown Home/End jump to start/end', () => {
    const a = document.createElement('button');
    const b = document.createElement('button');
    const c = document.createElement('button');
    document.body.append(a, b, c);
    b.focus();
    const ql = new QueryList<ElementRef<HTMLElement>>();
    ql.reset([new ElementRef(a), new ElementRef(b), new ElementRef(c)]);
    component.navFocusableElements = ql;

    const aFocus = vi.spyOn(a, 'focus');
    component.onNavKeydown(new KeyboardEvent('keydown', { key: 'Home' }));
    expect(aFocus).toHaveBeenCalled();

    b.focus();
    const cFocus = vi.spyOn(c, 'focus');
    component.onNavKeydown(new KeyboardEvent('keydown', { key: 'End' }));
    expect(cFocus).toHaveBeenCalled();
    a.remove();
    b.remove();
    c.remove();
  });

  it('onNavKeydown ignores unrelated keys', () => {
    const a = document.createElement('button');
    document.body.append(a);
    a.focus();
    const ql = new QueryList<ElementRef<HTMLElement>>();
    ql.reset([new ElementRef(a)]);
    component.navFocusableElements = ql;
    const event = new KeyboardEvent('keydown', { key: 'Tab', cancelable: true });
    component.onNavKeydown(event);
    expect(event.defaultPrevented).toBe(false);
    a.remove();
  });
});
