import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ExecutionTasksComponent } from './execution-tasks.component';

describe('ExecutionTasksComponent', () => {
  let component: ExecutionTasksComponent;
  let fixture: ComponentFixture<ExecutionTasksComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ExecutionTasksComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(ExecutionTasksComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
