import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ArchitectureResultsComponent } from './architecture-results.component';

describe('ArchitectureResultsComponent', () => {
  let component: ArchitectureResultsComponent;
  let fixture: ComponentFixture<ArchitectureResultsComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ArchitectureResultsComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(ArchitectureResultsComponent);
    component = fixture.componentInstance;
    component.data = {
      overview: '# Hello world',
      architecture_document: '## Doc',
      diagrams: { 'sequence': 'sequenceDiagram\n  A->>B: hi' },
      components: [],
      decisions: [],
    } as never;
    fixture.detectChanges();
  });

  it('creates and renders overviewHtml', () => {
    expect(component).toBeTruthy();
    expect(component.overviewHtml()).toBeTruthy();
    expect(component.architectureDocHtml()).toBeTruthy();
    expect(component.diagramEntries.length).toBe(1);
  });

  it('objectEntries converts record to entry array', () => {
    expect(component.objectEntries({ a: 1, b: 'x' })).toEqual([
      { key: 'a', value: 1 },
      { key: 'b', value: 'x' },
    ]);
  });

  it('isObjectDecision detects object', () => {
    expect(component.isObjectDecision({})).toBe(true);
    expect(component.isObjectDecision(null)).toBe(false);
    expect(component.isObjectDecision('s')).toBe(false);
  });

  it('getDecisionTitle returns id/title/name or empty', () => {
    expect(component.getDecisionTitle({ id: 'd1' })).toBe('d1');
    expect(component.getDecisionTitle({ title: 't' })).toBe('t');
    expect(component.getDecisionTitle({ name: 'n' })).toBe('n');
    expect(component.getDecisionTitle('s')).toBe('');
  });

  it('getDecisionDetails excludes id/title/name + nulls', () => {
    const result = component.getDecisionDetails({
      id: 'd1',
      title: 't',
      name: 'n',
      foo: 'bar',
      baz: null,
      empty: undefined,
      ok: 1,
    });
    expect(result).toEqual([
      { key: 'foo', value: 'bar' },
      { key: 'ok', value: 1 },
    ]);
  });

  it('getDecisionDetails returns empty array for non-object', () => {
    expect(component.getDecisionDetails(null)).toEqual([]);
    expect(component.getDecisionDetails('s')).toEqual([]);
  });
});
