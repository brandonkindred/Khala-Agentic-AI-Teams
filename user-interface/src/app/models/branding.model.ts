export interface Client {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
  contact_info?: string | null;
  notes?: string | null;
}

export interface ColorPalette {
  name: string;
  description: string;
  colors: string[];
  sentiment: string;
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
  color_inspiration?: string[];
  color_palettes?: ColorPalette[];
  selected_palette_index?: number | null;
  visual_style?: string;
  typography_preference?: string;
  interface_density?: string;
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
  conversation_id?: string | null;
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
  conversation_id?: string | null;
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

// Full TeamOutput types for brand preview
export interface BrandCodification {
  positioning_statement: string;
  brand_promise: string;
  brand_personality_traits: string[];
  narrative_pillars: string[];
}

export interface MoodBoardConcept {
  title: string;
  visual_direction: string;
  color_story: string[];
  typography_direction: string;
  image_style: string[];
}

export interface CreativeRefinementPlan {
  phases: string[];
  workshop_prompts: string[];
  decision_criteria: string[];
}

export interface WritingGuidelines {
  voice_principles: string[];
  style_dos: string[];
  style_donts: string[];
  editorial_quality_bar: string[];
}

export interface DesignSystemDefinition {
  design_principles: string[];
  foundation_tokens: string[];
  component_standards: string[];
}

export interface WikiEntry {
  title: string;
  summary: string;
  owners: string[];
  update_cadence: string;
}

export interface BrandBook {
  content: string;
  sections?: Record<string, unknown>;
}

/** Response from running the branding team for a brand. */
export interface BrandingTeamOutput {
  status: string;
  mission_summary: string;
  codification?: BrandCodification | null;
  mood_boards?: MoodBoardConcept[];
  creative_refinement?: CreativeRefinementPlan | null;
  writing_guidelines?: WritingGuidelines | null;
  brand_guidelines: string[];
  design_system?: DesignSystemDefinition | null;
  wiki_backlog?: WikiEntry[];
  brand_checks?: unknown[];
  human_feedback?: string | null;
  competitive_snapshot?: CompetitiveSnapshot | null;
  design_asset_result?: DesignAssetRequestResult | null;
  brand_book?: BrandBook | null;
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

// Conversation (chat) API types
export interface ConversationMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
}

export interface CreateConversationRequest {
  initial_message?: string | null;
  brand_id?: string | null;
}

export interface SendMessageRequest {
  message: string;
}

export interface ConversationStateResponse {
  conversation_id: string;
  brand_id?: string | null;
  messages: ConversationMessage[];
  mission: BrandingMissionSnapshot;
  latest_output: BrandingTeamOutput | null;
  suggested_questions: string[];
}

export interface ConversationSummary {
  conversation_id: string;
  brand_id?: string | null;
  brand_name?: string | null;
  created_at: string;
  updated_at: string;
  message_count: number;
}
