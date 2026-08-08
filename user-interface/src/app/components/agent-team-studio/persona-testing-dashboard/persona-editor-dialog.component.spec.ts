import { TestBed } from '@angular/core/testing';
import {
  MatDialogRef,
  MAT_DIALOG_DATA,
} from '@angular/material/dialog';
import { vi } from 'vitest';
import {
  PersonaEditorDialogComponent,
  PersonaEditorDialogData,
} from './persona-editor-dialog.component';
import type { PersonaInfo } from '../../../models';

function configure(data: PersonaEditorDialogData) {
  const ref = { close: vi.fn() };
  TestBed.configureTestingModule({
    imports: [PersonaEditorDialogComponent],
    providers: [
      { provide: MAT_DIALOG_DATA, useValue: data },
      { provide: MatDialogRef, useValue: ref },
    ],
  });
  return { fixture: TestBed.createComponent(PersonaEditorDialogComponent), ref };
}

describe('PersonaEditorDialogComponent', () => {
  beforeEach(() => TestBed.resetTestingModule());

  it('starts empty in create mode', () => {
    const { fixture } = configure({ mode: 'create' });
    const c = fixture.componentInstance;
    expect(c.name()).toBe('');
    expect(c.icon()).toBe('person');
    expect(c.isValid()).toBe(false);
  });

  it('pre-fills all fields from the persona in edit mode', () => {
    const persona: PersonaInfo = {
      id: 'p-1',
      name: 'QA',
      description: 'd',
      icon: 'bug_report',
      is_builtin: false,
      system_prompt: 'sp',
      spec_generation_prompt: 'gp',
      created_at: '',
      updated_at: '',
    };
    const { fixture } = configure({ mode: 'edit', persona });
    const c = fixture.componentInstance;
    expect(c.name()).toBe('QA');
    expect(c.description()).toBe('d');
    expect(c.icon()).toBe('bug_report');
    expect(c.systemPrompt()).toBe('sp');
    expect(c.specGenerationPrompt()).toBe('gp');
    expect(c.isValid()).toBe(true);
  });

  it('rejects submit when any field is blank', () => {
    const { fixture, ref } = configure({ mode: 'create' });
    const c = fixture.componentInstance;
    c.name.set('only name');
    c.submit();
    expect(ref.close).not.toHaveBeenCalled();
    expect(c.serverError()).toBeTruthy();
  });

  it('closes the dialog with the trimmed payload on submit', () => {
    const { fixture, ref } = configure({ mode: 'create' });
    const c = fixture.componentInstance;
    c.name.set('  QA  ');
    c.description.set('  desc  ');
    c.icon.set('bug_report');
    c.systemPrompt.set('full system prompt');
    c.specGenerationPrompt.set('full spec prompt');
    c.submit();
    expect(ref.close).toHaveBeenCalledWith({
      name: 'QA',
      description: 'desc',
      icon: 'bug_report',
      system_prompt: 'full system prompt',
      spec_generation_prompt: 'full spec prompt',
    });
  });

  it('cancel closes the dialog with no result', () => {
    const { fixture, ref } = configure({ mode: 'create' });
    fixture.componentInstance.cancel();
    expect(ref.close).toHaveBeenCalledWith();
  });
});
