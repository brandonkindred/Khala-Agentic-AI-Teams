import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ProductAnalysisRunFormComponent } from './product-analysis-run-form.component';

describe('ProductAnalysisRunFormComponent', () => {
  let component: ProductAnalysisRunFormComponent;
  let fixture: ComponentFixture<ProductAnalysisRunFormComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ProductAnalysisRunFormComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(ProductAnalysisRunFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('submit should emit when repoPath is set', () => {
    component.repoPath = '/repo';
    let emitted: any;
    component.submitRequest.subscribe((v) => (emitted = v));
    component.submit();
    expect(emitted).toEqual({ repo_path: '/repo' });
  });

  it('submit should include spec_content when set', () => {
    component.repoPath = '/repo';
    component.specContent = 'spec';
    let emitted: any;
    component.submitRequest.subscribe((v) => (emitted = v));
    component.submit();
    expect(emitted.spec_content).toBe('spec');
  });

  it('submit should not emit when repoPath is empty', () => {
    let emitted = false;
    component.submitRequest.subscribe(() => (emitted = true));
    component.submit();
    expect(emitted).toBe(false);
  });
});
