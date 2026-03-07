import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { AppShellComponent } from './app-shell.component';

describe('AppShellComponent', () => {
  let component: AppShellComponent;
  let fixture: ComponentFixture<AppShellComponent>;
  let routerSpy: { url: string };

  beforeEach(async () => {
    routerSpy = { url: '/dashboard' };
    await TestBed.configureTestingModule({
      imports: [AppShellComponent, NoopAnimationsModule],
      providers: [{ provide: Router, useValue: routerSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(AppShellComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('isActive should return true when router url starts with path', () => {
    expect(component.isActive('/dashboard')).toBe(true);
    expect(component.isActive('/')).toBe(true);
  });

  it('isActive should return false when router url does not start with path', () => {
    expect(component.isActive('/software-engineering')).toBe(false);
  });
});
