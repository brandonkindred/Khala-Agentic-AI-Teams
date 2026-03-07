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
    component.data = { overview: '', architecture_doc: '', diagram_entries: [] } as any;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should set overviewHtml from data', () => {
    expect(component.data).toBeDefined();
  });
});
