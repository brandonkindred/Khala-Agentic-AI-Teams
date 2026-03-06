export interface Client {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
  contact_info?: string | null;
  notes?: string | null;
}

export interface BrandingMissionSnapshot {
  company_name: string;
  company_description: string;
  target_audience: string;
  values?: string[];
  differentiators?: string[];
  desired_voice?: string;
  existing_brand_material?: string[];
  wiki_path?: string | null;
}

export interface BrandVersionSummary {
  version: number;
  created_at: string;
  status?: string | null;
}

export interface Brand {
  id: string;
  client_id: string;
  name: string;
  status: 'draft' | 'active' | 'evolving' | 'archived';
  mission: BrandingMissionSnapshot;
  latest_output?: unknown | null;
  version: number;
  history: BrandVersionSummary[];
  created_at: string;
  updated_at: string;
}

export interface CompetitiveSnapshot {
  summary: string;
  similar_brands: string[];
  insights: string[];
  source: string;
}

export interface DesignAssetRequestResult {
  request_id: string;
  status: string;
  artifacts: string[];
}

export interface BrandCheckRequest {
  asset_name: string;
  asset_description: string;
}

export interface RunBrandingTeamRequest {
  company_name: string;
  company_description: string;
  target_audience: string;
  values?: string[];
  differentiators?: string[];
  desired_voice?: string;
  existing_brand_material?: string[];
  wiki_path?: string | null;
  brand_checks?: BrandCheckRequest[];
  human_approved?: boolean;
  human_feedback?: string;
  client_id?: string | null;
  brand_id?: string | null;
}

export interface CreateClientRequest {
  name: string;
  contact_info?: string | null;
  notes?: string | null;
}

export interface CreateBrandRequest {
  company_name: string;
  company_description: string;
  target_audience: string;
  name?: string | null;
  values?: string[];
  differentiators?: string[];
  desired_voice?: string;
  existing_brand_material?: string[];
  wiki_path?: string | null;
}

export interface UpdateBrandRequest {
  company_name?: string;
  company_description?: string;
  target_audience?: string;
  name?: string | null;
  values?: string[] | null;
  differentiators?: string[] | null;
  desired_voice?: string | null;
  existing_brand_material?: string[] | null;
  wiki_path?: string | null;
  status?: string | null;
}

export interface RunBrandRequest {
  human_approved?: boolean;
  human_feedback?: string;
  include_market_research?: boolean;
  include_design_assets?: boolean;
  brand_checks?: BrandCheckRequest[];
}

/** Response from running the branding team for a brand. */
export interface BrandingTeamOutput {
  status: string;
  mission_summary: string;
  codification?: unknown;
  mood_boards?: unknown[];
  creative_refinement?: unknown;
  writing_guidelines?: unknown;
  brand_guidelines: string[];
  design_system?: unknown;
  wiki_backlog?: unknown[];
  brand_checks?: unknown[];
  human_feedback?: string | null;
  competitive_snapshot?: CompetitiveSnapshot | null;
  design_asset_result?: DesignAssetRequestResult | null;
  brand_book?: { content: string; sections?: Record<string, unknown> } | null;
}

export interface BrandingQuestion {
  id: string;
  question: string;
  context: string;
  target_field: string;
  status: string;
  answer?: string | null;
}

export interface BrandingSessionResponse {
  session_id: string;
  status: string;
  mission: RunBrandingTeamRequest;
  latest_output: {
    status: string;
    mission_summary: string;
    brand_guidelines: string[];
    writing_guidelines: {
      voice_principles: string[];
    };
  };
  open_questions: BrandingQuestion[];
  answered_questions: BrandingQuestion[];
}
