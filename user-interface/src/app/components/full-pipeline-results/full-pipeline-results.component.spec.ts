import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { FullPipelineResultsComponent } from './full-pipeline-results.component';

describe('FullPipelineResultsComponent', () => {
  let component: FullPipelineResultsComponent;
  let fixture: ComponentFixture<FullPipelineResultsComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [FullPipelineResultsComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(FullPipelineResultsComponent);
    component = fixture.componentInstance;
    component.data = { job_id: 'j1', status: 'completed' } as any;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should accept data input', () => {
    expect(component.data?.job_id).toBe('j1');
  });
});
