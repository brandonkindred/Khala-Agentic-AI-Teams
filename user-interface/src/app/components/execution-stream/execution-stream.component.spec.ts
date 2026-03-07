import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ExecutionStreamComponent } from './execution-stream.component';

describe('ExecutionStreamComponent', () => {
  let component: ExecutionStreamComponent;
  let fixture: ComponentFixture<ExecutionStreamComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ExecutionStreamComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(ExecutionStreamComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
