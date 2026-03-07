import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { PlanningV2PageComponent } from './planning-v2-page.component';

describe('PlanningV2PageComponent', () => {
  let component: PlanningV2PageComponent;
  let fixture: ComponentFixture<PlanningV2PageComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [PlanningV2PageComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(PlanningV2PageComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
