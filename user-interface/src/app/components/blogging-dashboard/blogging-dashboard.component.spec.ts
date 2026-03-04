import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { BloggingApiService } from '../../services/blogging-api.service';
import { BloggingDashboardComponent } from './blogging-dashboard.component';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

describe('BloggingDashboardComponent', () => {
  let component: BloggingDashboardComponent;
  let fixture: ComponentFixture<BloggingDashboardComponent>;
  let apiSpy: jasmine.SpyObj<BloggingApiService>;

  beforeEach(async () => {
    apiSpy = jasmine.createSpyObj('BloggingApiService', [
      'researchAndReview',
      'fullPipeline',
      'getJobs',
      'getJobStatus',
      'getJobArtifacts',
      'getJobArtifactContent',
    ]);
    apiSpy.getJobs.and.returnValue(of([]));

    await TestBed.configureTestingModule({
      imports: [BloggingDashboardComponent, NoopAnimationsModule],
      providers: [{ provide: BloggingApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(BloggingDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should fetch all jobs via getJobs(false) on init', () => {
    expect(apiSpy.getJobs).toHaveBeenCalledWith(false);
  });

  it('should call researchAndReview and set result on success', () => {
    const mockResult = {
      title_choices: [{ title: 'T', probability_of_success: 0.9 }],
      outline: 'Outline',
    };
    apiSpy.researchAndReview.and.returnValue(of(mockResult));

    component.onResearchReviewSubmit({ brief: 'Test', max_results: 20 });

    expect(apiSpy.researchAndReview).toHaveBeenCalledWith({
      brief: 'Test',
      max_results: 20,
    });
    expect(component.researchReviewResult).toEqual(mockResult);
    expect(component.loading).toBeFalse();
  });

  it('should set error on researchAndReview failure', () => {
    apiSpy.researchAndReview.and.returnValue(
      throwError(() => ({ error: { detail: 'Server error' } }))
    );

    component.onResearchReviewSubmit({ brief: 'Test', max_results: 20 });

    expect(component.error).toBeTruthy();
    expect(component.loading).toBeFalse();
  });
});
