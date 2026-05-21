import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { Component } from '@angular/core';
import { beforeEach } from 'vitest';
import { BreadcrumbComponent } from './breadcrumb.component';

@Component({ selector: 'app-foo', standalone: true, template: 'foo' })
class FooComponent {}

@Component({ selector: 'app-bar', standalone: true, template: 'bar' })
class BarComponent {}

describe('BreadcrumbComponent', () => {
  let component: BreadcrumbComponent;
  let fixture: ComponentFixture<BreadcrumbComponent>;
  let router: Router;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [BreadcrumbComponent],
      providers: [
        provideRouter([
          { path: 'foo', component: FooComponent, data: { breadcrumb: 'Foo' } },
          {
            path: 'parent',
            component: FooComponent,
            data: { breadcrumb: 'Parent' },
            children: [
              { path: 'child', component: BarComponent, data: { breadcrumb: 'Child' } },
              { path: 'no-crumb', component: BarComponent },
            ],
          },
        ]),
      ],
    }).compileComponents();
    router = TestBed.inject(Router);
  });

  it('creates with empty breadcrumbs by default', () => {
    fixture = TestBed.createComponent(BreadcrumbComponent);
    component = fixture.componentInstance;
    expect(component.breadcrumbs()).toEqual([]);
  });

  it('builds breadcrumbs after navigation', async () => {
    fixture = TestBed.createComponent(BreadcrumbComponent);
    component = fixture.componentInstance;
    await router.navigate(['/foo']);
    expect(component.breadcrumbs().length).toBeGreaterThanOrEqual(1);
    expect(component.breadcrumbs().some((c) => c.label === 'Foo')).toBe(true);
  });

  it('builds nested breadcrumbs from parent + child routes', async () => {
    fixture = TestBed.createComponent(BreadcrumbComponent);
    component = fixture.componentInstance;
    await router.navigate(['/parent/child']);
    const labels = component.breadcrumbs().map((c) => c.label);
    expect(labels).toContain('Parent');
    expect(labels).toContain('Child');
  });

  it('skips routes without breadcrumb data', async () => {
    fixture = TestBed.createComponent(BreadcrumbComponent);
    component = fixture.componentInstance;
    await router.navigate(['/parent/no-crumb']);
    const labels = component.breadcrumbs().map((c) => c.label);
    expect(labels).toContain('Parent');
    expect(labels).not.toContain(undefined);
  });
});
