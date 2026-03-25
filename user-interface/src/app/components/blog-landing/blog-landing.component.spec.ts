import { ComponentFixture, TestBed } from '@angular/core/testing';
import { BlogLandingComponent } from './blog-landing.component';
import { Router } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

describe('BlogLandingComponent', () => {
  let component: BlogLandingComponent;
  let fixture: ComponentFixture<BlogLandingComponent>;
  let router: Router;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [BlogLandingComponent, NoopAnimationsModule],
      providers: [
        {
          provide: Router,
          useValue: { navigate: vi.fn() },
        },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(BlogLandingComponent);
    component = fixture.componentInstance;
    router = TestBed.inject(Router);
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should navigate to dashboard on navigateToDashboard', () => {
    component.navigateToDashboard();
    expect(router.navigate).toHaveBeenCalledWith(['/blogging/dashboard']);
  });

  it('should navigate to full pipeline tab', () => {
    component.navigateToFullPipeline();
    expect(router.navigate).toHaveBeenCalledWith(['/blogging/dashboard'], { queryParams: { tab: 'full-pipeline' } });
  });

  it('should navigate to research tab', () => {
    component.navigateToResearch();
    expect(router.navigate).toHaveBeenCalledWith(['/blogging/dashboard'], { queryParams: { tab: 'research' } });
  });

  it('should have 8 pipeline phases', () => {
    expect(component.pipelinePhases.length).toBe(8);
  });

  it('should have 6 features', () => {
    expect(component.features.length).toBe(6);
  });

  it('should render hero title', () => {
    const compiled = fixture.nativeElement as HTMLElement;
    expect(compiled.querySelector('.hero-title')?.textContent).toContain('Write your first blog post');
  });

  it('should render pipeline steps', () => {
    const compiled = fixture.nativeElement as HTMLElement;
    const steps = compiled.querySelectorAll('.pipeline-step');
    expect(steps.length).toBe(8);
  });

  it('should render feature cards', () => {
    const compiled = fixture.nativeElement as HTMLElement;
    const cards = compiled.querySelectorAll('.feature-card');
    expect(cards.length).toBe(6);
  });
});
