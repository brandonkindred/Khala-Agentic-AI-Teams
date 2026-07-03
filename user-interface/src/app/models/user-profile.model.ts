/** Models for the cross-team User Profile feature (/api/user-profile). */

/** The single user profile. Single-tenant today (user_id === 'default'). */
export interface UserProfile {
  user_id: string;
  display_name: string;
  email: string;
  bio: string;
  preferences: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

/**
 * Partial update payload — omitted fields are left unchanged. Present scalar
 * fields are written verbatim; a present `preferences` dict is MERGED
 * key-by-key server-side (absent keys survive; no key-deletion path), so
 * send only the preference keys you own.
 */
export interface UserProfileUpdate {
  display_name?: string;
  email?: string;
  bio?: string;
  preferences?: Record<string, unknown>;
}

/** Canonical artifact-type strings shared with the backend registry. */
export type ArtifactType = 'brand' | 'blog_post' | 'project' | 'agentic_team';

/** A link between the profile and an artifact produced by some team. */
export interface Association {
  id: string;
  user_id: string;
  artifact_type: ArtifactType | string;
  team: string;
  artifact_id: string;
  label: string;
  role: string;
  created_at: string;
}

/** One row from GET /api/user-profile/integrations (pass-through). */
export interface ProfileIntegration {
  id: string;
  type: string;
  enabled: boolean;
  channel: string | null;
}

/** Aggregated payload from GET /api/user-profile/overview (one round-trip). */
export interface ProfileOverview {
  profile: UserProfile;
  associations: Association[];
  integrations: ProfileIntegration[];
}
