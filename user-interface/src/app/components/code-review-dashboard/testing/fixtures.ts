import type { PendingIssueProposal } from '../../../models/coding-team.model';
import type { GitHubPullRequestItem, GitHubRepoItem } from '../../../models/integrations.model';
import type { PrReviewRecord } from '../pr-review-record.model';

/** Shared test fixtures for the Code Review dashboard's specs — kept in one place so a
 * shape change to these models needs one edit instead of one per spec file. */

export function makePulls(count: number): GitHubPullRequestItem[] {
  return Array.from({ length: count }, (_, i) => ({
    number: i + 1,
    title: `PR ${i + 1}`,
    body_preview: `body ${i + 1}`,
    author: 'octocat',
    html_url: `https://example.com/pull/${i + 1}`,
    head: `feature-${i + 1}`,
    base: 'main',
    draft: i % 2 === 0,
    labels: i % 2 === 0 ? ['needs-review'] : [],
    updated_at: '2026-01-01T00:00:00Z',
  }));
}

export function makeReviewRecord(over: Partial<PrReviewRecord> = {}): PrReviewRecord {
  return {
    jobId: 'j1',
    prNumber: 1,
    owner: 'acme',
    repo: 'widgets',
    startedAt: Date.parse('2026-01-01T00:00:00Z'),
    status: 'running',
    ...over,
  };
}

export const REPO: GitHubRepoItem = {
  owner: 'acme',
  name: 'widgets',
  full_name: 'acme/widgets',
  private: false,
  archived: false,
  html_url: 'https://github.com/acme/widgets',
  description: 'Widget factory',
  default_branch: 'main',
  open_issues_count: 3,
  pushed_at: '2026-06-09T10:00:00Z',
};

export function makeProposal(id: string, over: Record<string, unknown> = {}): PendingIssueProposal {
  return {
    id,
    severity: 'high',
    category: 'logic',
    file_path: 'a.py',
    line: 3,
    description: `bug ${id}`,
    suggestion: 'fix',
    issue_number: null,
    issue_url: null,
    ...over,
  };
}

export function terminalReviewRecordWith(proposals: PendingIssueProposal[]): PrReviewRecord {
  return makeReviewRecord({
    status: 'completed',
    reviewSummary: {
      total_issues: 0,
      inline_comments: 0,
      event: 'COMMENT',
      pending_issue_proposals: proposals,
    },
  });
}
