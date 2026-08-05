import { TestBed } from '@angular/core/testing';
import {
  MatDialogRef,
  MAT_DIALOG_DATA,
} from '@angular/material/dialog';
import { vi } from 'vitest';
import {
  StartTestDialogComponent,
  StartTestDialogData,
} from './start-test-dialog.component';
import type { PersonaInfo, TestableTeam } from '../../../models';

const persona = (id: string): PersonaInfo => ({
  id,
  name: id,
  description: '',
  icon: 'person',
  is_builtin: false,
  system_prompt: '',
  spec_generation_prompt: '',
  created_at: '',
  updated_at: '',
});

const team = (key: string): TestableTeam => ({ team_key: key, display_name: key });

function configure(data: StartTestDialogData) {
  const ref = { close: vi.fn() };
  TestBed.configureTestingModule({
    imports: [StartTestDialogComponent],
    providers: [
      { provide: MAT_DIALOG_DATA, useValue: data },
      { provide: MatDialogRef, useValue: ref },
    ],
  });
  return { fixture: TestBed.createComponent(StartTestDialogComponent), ref };
}

describe('StartTestDialogComponent', () => {
  beforeEach(() => TestBed.resetTestingModule());

  it('pre-selects the first persona and the only team if just one is testable', () => {
    const { fixture } = configure({
      personas: [persona('p-1'), persona('p-2')],
      teams: [team('software_engineering')],
    });
    const c = fixture.componentInstance;
    expect(c.personaId()).toBe('p-1');
    expect(c.targetTeamKey()).toBe('software_engineering');
    expect(c.isValid()).toBe(true);
  });

  it('respects initialPersonaId when provided', () => {
    const { fixture } = configure({
      personas: [persona('p-1'), persona('p-2')],
      teams: [team('software_engineering')],
      initialPersonaId: 'p-2',
    });
    expect(fixture.componentInstance.personaId()).toBe('p-2');
  });

  it('does not pre-select team when multiple options are available', () => {
    const { fixture } = configure({
      personas: [persona('p-1')],
      teams: [team('software_engineering'), team('planning')],
    });
    expect(fixture.componentInstance.targetTeamKey()).toBe('');
    expect(fixture.componentInstance.isValid()).toBe(false);
  });

  it('closes with the full payload on submit', () => {
    const { fixture, ref } = configure({
      personas: [persona('p-1')],
      teams: [team('software_engineering')],
    });
    const c = fixture.componentInstance;
    c.projectName.set(' taskflow-mvp ');
    c.submit();
    expect(ref.close).toHaveBeenCalledWith({
      persona_id: 'p-1',
      target_team_key: 'software_engineering',
      project_name: 'taskflow-mvp',
    });
  });

  it('omits project_name when blank, so the server picks the default', () => {
    const { fixture, ref } = configure({
      personas: [persona('p-1')],
      teams: [team('software_engineering')],
    });
    const c = fixture.componentInstance;
    c.projectName.set('   ');
    c.submit();
    expect(ref.close).toHaveBeenCalledWith({
      persona_id: 'p-1',
      target_team_key: 'software_engineering',
      project_name: undefined,
    });
  });

  it('does nothing on submit when invalid', () => {
    const { fixture, ref } = configure({
      personas: [],
      teams: [],
    });
    fixture.componentInstance.submit();
    expect(ref.close).not.toHaveBeenCalled();
  });
});
