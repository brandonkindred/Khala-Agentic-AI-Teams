import { of } from 'rxjs';
import { vi } from 'vitest';

/**
 * Shared TeamAssistantApiService test double.
 *
 * The team dashboards embed `<app-team-assistant-chat [teamApiUrl]>`, which
 * loads a conversation on init via TeamAssistantApiService. Specs that render a
 * dashboard need every method the chat's render path can touch stubbed, or the
 * embedded component throws. This factory returns a fresh stub (independent
 * `vi.fn()`s) per call so specs don't share mock state.
 */
export const createTeamAssistantApiMock = () => {
  const state = {
    conversation_id: 'c1',
    messages: [{ role: 'assistant', content: 'hi', timestamp: '2025-01-01T00:00:00Z' }],
    context: {},
    suggested_questions: [],
  };
  return {
    getConversation: vi.fn().mockReturnValue(of(state)),
    sendMessage: vi.fn().mockReturnValue(of(state)),
    updateContext: vi.fn().mockReturnValue(of(state)),
    getReadiness: vi.fn().mockReturnValue(of({ ready: false, missing_fields: [] })),
    launch: vi.fn().mockReturnValue(of({ job_id: 'j1', conversation_id: 'c1', upstream_status: 200, upstream_body: {} })),
    resetConversation: vi.fn().mockReturnValue(of(state)),
  };
};
