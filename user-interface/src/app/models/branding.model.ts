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
