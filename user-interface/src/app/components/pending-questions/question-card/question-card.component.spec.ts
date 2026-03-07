import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { QuestionCardComponent } from './question-card.component';

describe('QuestionCardComponent', () => {
  let component: QuestionCardComponent;
  let fixture: ComponentFixture<QuestionCardComponent>;

  const mockQuestion = {
    id: 'q1',
    question: 'Choose one?',
    required: true,
    options: [{ id: 'a1', label: 'A1' }, { id: 'other', label: 'Other' }],
  };

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [QuestionCardComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(QuestionCardComponent);
    component = fixture.componentInstance;
    component.question = mockQuestion as any;
    component.questionIndex = 0;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('onOptionToggle should emit optionToggled', () => {
    let emitted: any;
    component.optionToggled.subscribe((v) => (emitted = v));
    component.onOptionToggle('a1', true);
    expect(emitted).toEqual({ optionId: 'a1', checked: true });
    expect(component.selectedOptionIds.has('a1')).toBe(true);
  });

  it('isOptionSelected should return true when selected', () => {
    component.selectedOptionIds.add('a1');
    expect(component.isOptionSelected('a1')).toBe(true);
  });
});
