import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ResearchReviewResultsComponent } from './research-review-results.component';

describe('ResearchReviewResultsComponent', () => {
  let component: ResearchReviewResultsComponent;
  let fixture: ComponentFixture<ResearchReviewResultsComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ResearchReviewResultsComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(ResearchReviewResultsComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
