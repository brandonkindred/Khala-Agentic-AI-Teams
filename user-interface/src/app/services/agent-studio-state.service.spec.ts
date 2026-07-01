import { TestBed } from '@angular/core/testing';
import { AgentStudioStateService } from './agent-studio-state.service';

describe('AgentStudioStateService', () => {
  let service: AgentStudioStateService;

  beforeEach(() => {
    service = TestBed.runInInjectionContext(() => new AgentStudioStateService());
  });

  it('starts at stage 0 with empty handoff state', () => {
    expect(service.activeStage()).toBe(0);
    expect(service.maxReachedStage()).toBe(0);
    expect(service.canAdvance()).toBe(true);
    expect(service.registryAgentId()).toBeNull();
    expect(service.teamId()).toBeNull();
    expect(service.processId()).toBeNull();
    expect(service.personaId()).toBeNull();
    expect(service.draftAgentId()).toBeNull();
    expect(service.handoff()).toEqual({
      registryAgentId: null,
      teamId: null,
      processId: null,
      personaId: null,
      draftAgentId: null,
    });
  });

  it('stageStatus reports active / todo / done relative to the active stage', () => {
    expect(service.stageStatus(0)).toBe('active');
    expect(service.stageStatus(1)).toBe('todo');
    service.navigateToStage(2);
    expect(service.stageStatus(0)).toBe('done');
    expect(service.stageStatus(1)).toBe('done');
    expect(service.stageStatus(2)).toBe('active');
    expect(service.stageStatus(3)).toBe('todo');
  });

  it('navigateToStage moves the active stage and raises maxReachedStage', () => {
    service.navigateToStage(3);
    expect(service.activeStage()).toBe(3);
    expect(service.maxReachedStage()).toBe(3);
    expect(service.canAdvance()).toBe(false);
  });

  it('navigating backward keeps the furthest-reached stage', () => {
    service.navigateToStage(3);
    service.navigateToStage(1);
    expect(service.activeStage()).toBe(1);
    expect(service.maxReachedStage()).toBe(3);
  });

  it('navigateToStage rejects out-of-range and non-integer indices', () => {
    expect(() => service.navigateToStage(-1)).toThrow(RangeError);
    expect(() => service.navigateToStage(4)).toThrow(RangeError);
    expect(() => service.navigateToStage(1.5)).toThrow(RangeError);
    // Rejected calls leave state untouched.
    expect(service.activeStage()).toBe(0);
  });

  it('stageStatus enforces its index precondition rather than returning todo', () => {
    expect(() => service.stageStatus(-1)).toThrow(RangeError);
    expect(() => service.stageStatus(4)).toThrow(RangeError);
    expect(() => service.stageStatus(2.5)).toThrow(RangeError);
  });

  it('advance steps forward and is a no-op at the last stage', () => {
    service.advance();
    expect(service.activeStage()).toBe(1);
    service.navigateToStage(3);
    service.advance();
    expect(service.activeStage()).toBe(3);
  });

  it('setters update each handoff signal and the snapshot', () => {
    service.setRegistryAgentId('reg-1');
    service.setTeamId('team-1');
    service.setProcessId('proc-1');
    service.setPersonaId('persona-1');
    service.setDraftAgentId('draft-1');
    expect(service.handoff()).toEqual({
      registryAgentId: 'reg-1',
      teamId: 'team-1',
      processId: 'proc-1',
      personaId: 'persona-1',
      draftAgentId: 'draft-1',
    });
  });

  it('reset clears handoff state and returns to stage 0', () => {
    service.setRegistryAgentId('reg-1');
    service.setDraftAgentId('draft-1');
    service.navigateToStage(3);
    service.reset();
    expect(service.activeStage()).toBe(0);
    expect(service.maxReachedStage()).toBe(0);
    expect(service.registryAgentId()).toBeNull();
    expect(service.draftAgentId()).toBeNull();
    expect(service.handoff()).toEqual({
      registryAgentId: null,
      teamId: null,
      processId: null,
      personaId: null,
      draftAgentId: null,
    });
  });

  it('starts with the Stage-3 gate signals unstaffed/unset', () => {
    expect(service.rosterFullyStaffed()).toBe(false);
    expect(service.composeProcessStatus()).toBeNull();
  });

  it('setRosterFullyStaffed / setComposeProcessStatus update the Stage-3 gate signals', () => {
    service.setRosterFullyStaffed(true);
    service.setComposeProcessStatus('complete');
    expect(service.rosterFullyStaffed()).toBe(true);
    expect(service.composeProcessStatus()).toBe('complete');
  });

  it('reset clears the Stage-3 gate signals', () => {
    service.setRosterFullyStaffed(true);
    service.setComposeProcessStatus('complete');
    service.reset();
    expect(service.rosterFullyStaffed()).toBe(false);
    expect(service.composeProcessStatus()).toBeNull();
  });
});
