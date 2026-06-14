import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { QuestionCardComponent } from './question-card.component';

describe('QuestionCardComponent', () => {
  let component: QuestionCardComponent;
  let fixture: ComponentFixture<QuestionCardComponent>;

  const mockQuestion = {
    id: 'q1',
    question: 'Choose one?',
    required: true,
    allow_multiple: false,
    options: [{ id: 'a1', label: 'A1' }, { id: 'b1', label: 'B1' }, { id: 'other', label: 'Other' }],
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

  it('creates', () => {
    expect(component).toBeTruthy();
  });

  it('isMultiSelect reflects question.allow_multiple', () => {
    expect(component.isMultiSelect).toBe(false);
    component.question = { ...mockQuestion, allow_multiple: true } as any;
    expect(component.isMultiSelect).toBe(true);
  });

  it('onOptionToggle single-select clears others when checking', () => {
    component.selectedOptionIds.add('b1');
    component.otherText = 'foo';
    component.onOptionToggle('a1', true);
    expect(component.selectedOptionIds.size).toBe(1);
    expect(component.selectedOptionIds.has('a1')).toBe(true);
    expect(component.otherText).toBe('');
  });

  it('onOptionToggle multi-select adds without clearing', () => {
    component.question = { ...mockQuestion, allow_multiple: true } as any;
    component.selectedOptionIds.add('b1');
    component.onOptionToggle('a1', true);
    expect(component.selectedOptionIds.has('a1')).toBe(true);
    expect(component.selectedOptionIds.has('b1')).toBe(true);
  });

  it('onOptionToggle uncheck removes', () => {
    component.selectedOptionIds.add('a1');
    component.onOptionToggle('a1', false);
    expect(component.selectedOptionIds.has('a1')).toBe(false);
  });

  it('onOptionToggle clearing "other" resets otherText', () => {
    component.selectedOptionIds.add('other');
    component.otherText = 'detail';
    component.onOptionToggle('other', false);
    expect(component.otherText).toBe('');
  });

  it('onRadioChange clears all + selects target', () => {
    component.selectedOptionIds.add('b1');
    component.otherText = 'foo';
    const spy = vi.fn();
    component.optionToggled.subscribe(spy);
    component.onRadioChange('a1');
    expect(component.selectedOptionIds.size).toBe(1);
    expect(component.selectedOptionIds.has('a1')).toBe(true);
    expect(component.otherText).toBe('');
    expect(spy).toHaveBeenCalledWith({ optionId: 'a1', checked: true });
  });

  it('isOptionSelected', () => {
    component.selectedOptionIds.add('a1');
    expect(component.isOptionSelected('a1')).toBe(true);
    expect(component.isOptionSelected('b1')).toBe(false);
  });

  it('isOtherSelected', () => {
    expect(component.isOtherSelected()).toBe(false);
    component.selectedOptionIds.add('other');
    expect(component.isOtherSelected()).toBe(true);
  });

  it('onOtherTextChange emits', () => {
    const spy = vi.fn();
    component.otherTextChanged.subscribe(spy);
    component.onOtherTextChange('detail');
    expect(spy).toHaveBeenCalledWith('detail');
    expect(component.otherText).toBe('detail');
  });

  it('onAutoAnswerRequest/Apply/Dismiss emit', () => {
    const reqSpy = vi.fn();
    const applySpy = vi.fn();
    const dismissSpy = vi.fn();
    component.autoAnswerRequested.subscribe(reqSpy);
    component.autoAnswerApplied.subscribe(applySpy);
    component.autoAnswerDismissed.subscribe(dismissSpy);
    component.onAutoAnswerRequest();
    component.onApplyAutoAnswer();
    component.onDismissAutoAnswer();
    expect(reqSpy).toHaveBeenCalled();
    expect(applySpy).toHaveBeenCalled();
    expect(dismissSpy).toHaveBeenCalled();
  });

  it('getConfidenceLabel bands', () => {
    expect(component.getConfidenceLabel(0.9)).toBe('High');
    expect(component.getConfidenceLabel(0.7)).toBe('Medium');
    expect(component.getConfidenceLabel(0.3)).toBe('Low');
  });

  it('getSelectedOptionIds + hasSelections', () => {
    expect(component.hasSelections()).toBe(false);
    component.selectedOptionIds.add('a1');
    component.selectedOptionIds.add('b1');
    expect(component.hasSelections()).toBe(true);
    expect(component.getSelectedOptionIds().sort()).toEqual(['a1', 'b1']);
  });

  it('renders the auto-answer button by default', () => {
    const btn = fixture.nativeElement.querySelector('.auto-answer-btn');
    expect(btn).toBeTruthy();
  });

  it('hides the auto-answer button when autoAnswerEnabled is false', () => {
    fixture.componentRef.setInput('autoAnswerEnabled', false);
    fixture.detectChanges();
    const btn = fixture.nativeElement.querySelector('.auto-answer-btn');
    expect(btn).toBeFalsy();
  });
});
