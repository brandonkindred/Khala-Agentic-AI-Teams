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

  it('starts with no server draft bound', () => {
    expect(service.currentDraftId()).toBeNull();
    expect(service.currentDraftName()).toBeNull();
  });

  it('setCurrentDraft updates the id and name together', () => {
    service.setCurrentDraft('draft-1', 'My draft');
    expect(service.currentDraftId()).toBe('draft-1');
    expect(service.currentDraftName()).toBe('My draft');
  });

  it('reset clears the bound server draft', () => {
    service.setCurrentDraft('draft-1', 'My draft');
    service.reset();
    expect(service.currentDraftId()).toBeNull();
    expect(service.currentDraftName()).toBeNull();
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

  it('tracks consumed handoff keys and does not conflate distinct keys', () => {
    expect(service.hasConsumedHandoff('t-1::a')).toBe(false);
    service.markHandoffConsumed('t-1::a');
    expect(service.hasConsumedHandoff('t-1::a')).toBe(true);
    // A different (team, agent) key is unaffected.
    expect(service.hasConsumedHandoff('t-2::a')).toBe(false);
  });

  it('reset clears consumed handoff keys', () => {
    service.markHandoffConsumed('t-1::a');
    service.reset();
    expect(service.hasConsumedHandoff('t-1::a')).toBe(false);
  });

  describe('Stage-1 build sub-stepper', () => {
    it('starts at sub-stage 0 (Start)', () => {
      expect(service.activeBuildSubStage()).toBe(0);
      expect(service.maxReachedBuildSubStage()).toBe(0);
      expect(service.canAdvanceBuildSubStage()).toBe(true);
    });

    it('buildSubStageStatus reports active / todo / done relative to the active sub-stage', () => {
      expect(service.buildSubStageStatus(0)).toBe('active');
      expect(service.buildSubStageStatus(1)).toBe('todo');
      expect(service.buildSubStageStatus(2)).toBe('todo');
      service.advanceBuildSubStage();
      expect(service.buildSubStageStatus(0)).toBe('done');
      expect(service.buildSubStageStatus(1)).toBe('active');
      expect(service.buildSubStageStatus(2)).toBe('todo');
    });

    it('buildSubStageStatus rejects out-of-range and non-integer indices', () => {
      expect(() => service.buildSubStageStatus(-1)).toThrow(RangeError);
      expect(() => service.buildSubStageStatus(3)).toThrow(RangeError);
      expect(() => service.buildSubStageStatus(1.5)).toThrow(RangeError);
    });

    it('advanceBuildSubStage steps forward one sub-stage at a time and is a no-op at Configure', () => {
      service.advanceBuildSubStage();
      expect(service.activeBuildSubStage()).toBe(1);
      expect(service.maxReachedBuildSubStage()).toBe(1);
      service.advanceBuildSubStage();
      expect(service.activeBuildSubStage()).toBe(2);
      expect(service.canAdvanceBuildSubStage()).toBe(false);
      service.advanceBuildSubStage();
      expect(service.activeBuildSubStage()).toBe(2);
    });

    it('backToDefine moves Configure back to Define without lowering maxReachedBuildSubStage', () => {
      service.advanceBuildSubStage();
      service.advanceBuildSubStage();
      expect(service.activeBuildSubStage()).toBe(2);
      service.backToDefine();
      expect(service.activeBuildSubStage()).toBe(1);
      expect(service.maxReachedBuildSubStage()).toBe(2);
    });

    it('backToDefine rejects being called from any sub-stage other than Configure', () => {
      expect(() => service.backToDefine()).toThrow(RangeError);
      service.advanceBuildSubStage();
      expect(() => service.backToDefine()).toThrow(RangeError);
      expect(service.activeBuildSubStage()).toBe(1);
    });

    it('reset clears the sub-stepper back to Start', () => {
      service.advanceBuildSubStage();
      service.advanceBuildSubStage();
      service.reset();
      expect(service.activeBuildSubStage()).toBe(0);
      expect(service.maxReachedBuildSubStage()).toBe(0);
    });

    it('resetBuildSubStage clears the sub-stepper back to Start without touching handoff state', () => {
      service.setRegistryAgentId('reg-1');
      service.advanceBuildSubStage();
      service.advanceBuildSubStage();
      service.resetBuildSubStage();
      expect(service.activeBuildSubStage()).toBe(0);
      expect(service.maxReachedBuildSubStage()).toBe(0);
      expect(service.registryAgentId()).toBe('reg-1');
    });
  });

  describe('dirty tracking', () => {
    it('starts clean on a blank session', () => {
      expect(service.isDirty()).toBe(false);
    });

    it('becomes dirty when any handoff id is set and no snapshot exists yet', () => {
      service.setRegistryAgentId('reg-1');
      expect(service.isDirty()).toBe(true);
    });

    it('markClean makes the current handoff clean', () => {
      service.setTeamId('team-1');
      service.markClean();
      expect(service.isDirty()).toBe(false);
    });

    it('markSaved records a snapshot that may differ from the current handoff', () => {
      service.setTeamId('team-1');
      service.markSaved({
        registryAgentId: null,
        teamId: 'team-1',
        processId: null,
        personaId: null,
        draftAgentId: null,
      });
      expect(service.isDirty()).toBe(false);
      service.setTeamId('team-2');
      service.markSaved({
        registryAgentId: null,
        teamId: 'team-1',
        processId: null,
        personaId: null,
        draftAgentId: null,
      });
      expect(service.isDirty()).toBe(true);
      expect(service.teamId()).toBe('team-2');
    });

    it('becomes dirty again when an id changes after markClean', () => {
      service.setTeamId('team-1');
      service.markClean();
      service.setTeamId('team-2');
      expect(service.isDirty()).toBe(true);
    });

    it('reset returns to a clean blank session', () => {
      service.setRegistryAgentId('reg-1');
      service.markClean();
      service.setTeamId('team-1');
      service.reset();
      expect(service.isDirty()).toBe(false);
      expect(service.handoff()).toEqual({
        registryAgentId: null,
        teamId: null,
        processId: null,
        personaId: null,
        draftAgentId: null,
      });
    });

    it('setCurrentDraft does not by itself change isDirty', () => {
      service.setRegistryAgentId('reg-1');
      service.markClean();
      service.setCurrentDraft('d-1', 'My draft');
      expect(service.isDirty()).toBe(false);
    });

    it('invalidateSavedSnapshot makes a retained handoff dirty without clearing ids', () => {
      service.setRegistryAgentId('reg-1');
      service.markClean();
      service.invalidateSavedSnapshot();
      expect(service.isDirty()).toBe(true);
      expect(service.registryAgentId()).toBe('reg-1');
    });

    it('invalidateSavedSnapshot on a blank session stays clean', () => {
      service.invalidateSavedSnapshot();
      expect(service.isDirty()).toBe(false);
    });

    it('compares all five ids, not just the one that was last written', () => {
      service.setRegistryAgentId('reg-1');
      service.setDraftAgentId('draft-1');
      service.markClean();
      service.setPersonaId('persona-1');
      expect(service.isDirty()).toBe(true);
    });
  });
});
