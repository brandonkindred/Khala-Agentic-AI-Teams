/**
 * Models for the `product_delivery` team — mirror of
 * `backend/agents/product_delivery/models.py`.
 *
 * The backlog tree shape (`BacklogTree → InitiativeNode → EpicNode →
 * StoryNode`) is the read-projection returned by
 * `GET /api/product-delivery/products/{id}/backlog` and is used by the
 * Agent Console's Backlog tab.
 */

/** Free-form status string (1–40 chars, whitespace-stripped). */
export type StatusString = string;

/** Grooming method — drives which score column the API ranks against. */
export type GroomMethod = 'wsjf' | 'rice';

/** Audit fields present on every persisted row. */
export interface AuditedRow {
  id: string;
  author: string;
  /** ISO-8601 timestamp string. */
  created_at: string;
  /** ISO-8601 timestamp string. */
  updated_at: string;
}

export interface Product extends AuditedRow {
  name: string;
  description: string;
  vision: string;
}

interface ScoredRow extends AuditedRow {
  title: string;
  summary: string;
  status: StatusString;
  wsjf_score: number | null;
  rice_score: number | null;
}

export interface Initiative extends ScoredRow {
  product_id: string;
}

export interface Epic extends ScoredRow {
  initiative_id: string;
}

export interface Story extends ScoredRow {
  epic_id: string;
  user_story: string;
  estimate_points: number | null;
}

export interface Task extends AuditedRow {
  story_id: string;
  title: string;
  description: string;
  status: StatusString;
  owner: string | null;
}

export interface AcceptanceCriterion extends AuditedRow {
  story_id: string;
  text: string;
  satisfied: boolean;
}

export interface StoryNode extends Story {
  tasks: Task[];
  acceptance_criteria: AcceptanceCriterion[];
}

export interface EpicNode extends Epic {
  stories: StoryNode[];
}

export interface InitiativeNode extends Initiative {
  epics: EpicNode[];
}

export interface BacklogTree {
  product: Product;
  initiatives: InitiativeNode[];
}

export interface FeedbackItem extends AuditedRow {
  product_id: string;
  source: string;
  raw_payload: Record<string, unknown>;
  severity: string;
  status: StatusString;
  linked_story_id: string | null;
  sprint_id: string | null;
}

export interface Sprint extends AuditedRow {
  product_id: string;
  name: string;
  capacity_points: number;
  starts_at: string | null;
  ends_at: string | null;
  status: StatusString;
}

export interface SprintWithStories {
  sprint: Sprint;
  stories: Story[];
  /** Map of `story_id` → its acceptance criteria. */
  acceptance_criteria_by_story_id: Record<string, AcceptanceCriterion[]>;
}

export interface Release extends AuditedRow {
  sprint_id: string;
  version: string;
  notes_path: string;
  shipped_at: string | null;
}

/** Per-item entry in a `GroomResult.ranked` list. */
export interface RankedBacklogItem {
  kind: 'initiative' | 'epic' | 'story';
  id: string;
  title: string;
  /** The composite score the row was ranked by (matches `method`). */
  score: number | null;
  wsjf_score: number | null;
  rice_score: number | null;
  /** Per-item rationale — populated by `ProductOwnerAgent`. May be empty. */
  rationale: string;
}

export interface GroomResult {
  product_id: string;
  method: GroomMethod;
  ranked: RankedBacklogItem[];
  /** Top-level rationale describing the overall ranking pass. */
  rationale: string;
}

export interface SprintPlanResult {
  sprint_id: string;
  selected_story_ids: string[];
  skipped_story_ids: string[];
  used_capacity: number;
  remaining_capacity: number;
  rationale: string;
}

// ---------------------------------------------------------------------------
// Request bodies
// ---------------------------------------------------------------------------

export interface ProductCreate {
  name: string;
  description?: string;
  vision?: string;
}

export interface StatusUpdate {
  status: StatusString;
}

export interface ScoreUpdate {
  wsjf_score?: number | null;
  rice_score?: number | null;
}

export interface GroomRequest {
  product_id: string;
  method?: GroomMethod;
  /** When `false`, returns the ranked list without writing scores. */
  persist?: boolean;
}

export interface FeedbackLinkUpdate {
  /** `null` clears the link. Field must be present (no optional). */
  linked_story_id: string | null;
}

export interface SprintPlanRequest {
  capacity_points?: number | null;
}
