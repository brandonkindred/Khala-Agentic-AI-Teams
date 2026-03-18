/** TypeScript models for the AI Sales Team API. */

// -------------------------------------------------------------------------
// Enums (string literal unions matching backend Python enums)
// -------------------------------------------------------------------------

export type PipelineStage =
  | 'prospecting'
  | 'outreach'
  | 'qualification'
  | 'nurturing'
  | 'discovery'
  | 'proposal'
  | 'negotiation'
  | 'closed_won'
  | 'closed_lost';

export type OutreachChannel = 'email' | 'phone' | 'linkedin' | 'video';

export type CloseType =
  | 'assumptive'
  | 'summary'
  | 'urgency'
  | 'alternative_choice'
  | 'sharp_angle'
  | 'feel_felt_found';

export type ForecastCategory = 'pipeline' | 'best_case' | 'commit' | 'closed' | 'omitted';

export type OutcomeResult = 'converted' | 'stalled' | 'objection' | 'disqualified' | 'won' | 'lost';

// -------------------------------------------------------------------------
// ICP & Prospect
// -------------------------------------------------------------------------

export interface IdealCustomerProfile {
  industry: string[];
  company_size_min: number;
  company_size_max: number;
  job_titles: string[];
  pain_points: string[];
  budget_range_usd: string;
  geographic_focus: string[];
  tech_stack_keywords: string[];
  disqualifying_traits: string[];
}

export interface Prospect {
  company_name: string;
  website?: string;
  contact_name?: string;
  contact_title?: string;
  contact_email?: string;
  linkedin_url?: string;
  company_size_estimate?: string;
  industry?: string;
  icp_match_score: number;
  research_notes: string;
  trigger_events: string[];
}

// -------------------------------------------------------------------------
// Outreach
// -------------------------------------------------------------------------

export interface EmailTouch {
  day: number;
  subject_line: string;
  body: string;
  personalization_tokens: string[];
  call_to_action: string;
}

export interface OutreachSequence {
  prospect: Prospect;
  email_sequence: EmailTouch[];
  call_script: string;
  linkedin_message: string;
  sequence_rationale: string;
}

// -------------------------------------------------------------------------
// Qualification
// -------------------------------------------------------------------------

export interface BANTScore {
  budget: number;
  authority: number;
  need: number;
  timeline: number;
}

export interface MEDDICScore {
  metrics_identified: boolean;
  economic_buyer_known: boolean;
  decision_criteria_understood: boolean;
  decision_process_mapped: boolean;
  identify_pain: boolean;
  champion_found: boolean;
}

export interface QualificationScore {
  prospect: Prospect;
  bant: BANTScore;
  meddic: MEDDICScore;
  overall_score: number;
  value_creation_level: number;
  recommended_action: string;
  disqualification_reason?: string;
  qualification_notes: string;
}

// -------------------------------------------------------------------------
// Nurture
// -------------------------------------------------------------------------

export interface NurtureTouchpoint {
  day: number;
  channel: OutreachChannel;
  content_type: string;
  message: string;
  goal: string;
}

export interface NurtureSequence {
  prospect: Prospect;
  duration_days: number;
  touchpoints: NurtureTouchpoint[];
  re_engagement_triggers: string[];
  content_recommendations: string[];
}

// -------------------------------------------------------------------------
// Discovery
// -------------------------------------------------------------------------

export interface SPINQuestions {
  situation: string[];
  problem: string[];
  implication: string[];
  need_payoff: string[];
}

export interface DiscoveryPlan {
  prospect: Prospect;
  spin_questions: SPINQuestions;
  challenger_insight: string;
  demo_agenda: string[];
  expected_objections: string[];
  success_criteria_for_call: string;
}

// -------------------------------------------------------------------------
// Proposal
// -------------------------------------------------------------------------

export interface ROIModel {
  annual_cost_usd: number;
  estimated_annual_benefit_usd: number;
  payback_months: number;
  roi_percentage: number;
  assumptions: string[];
}

export interface ProposalSection {
  heading: string;
  content: string;
}

export interface SalesProposal {
  prospect: Prospect;
  executive_summary: string;
  situation_analysis: string;
  proposed_solution: string;
  roi_model: ROIModel;
  investment_table: string;
  implementation_timeline: string;
  risk_mitigation: string;
  next_steps: string[];
  custom_sections: ProposalSection[];
}

// -------------------------------------------------------------------------
// Closing
// -------------------------------------------------------------------------

export interface ObjectionHandler {
  objection: string;
  response: string;
  feel_felt_found?: string;
}

export interface ClosingStrategy {
  prospect: Prospect;
  recommended_close_technique: CloseType;
  close_script: string;
  objection_handlers: ObjectionHandler[];
  urgency_framing: string;
  walk_away_criteria: string;
  emotional_intelligence_notes: string;
}

// -------------------------------------------------------------------------
// Coaching
// -------------------------------------------------------------------------

export interface DealRiskSignal {
  signal: string;
  severity: 'low' | 'medium' | 'high';
  recommended_action: string;
}

export interface PipelineCoachingReport {
  prospects_reviewed: number;
  deal_risk_signals: DealRiskSignal[];
  talk_listen_ratio_advice: string;
  velocity_insights: string;
  forecast_category: ForecastCategory;
  top_priority_deals: string[];
  recommended_next_actions: string[];
  coaching_summary: string;
}

// -------------------------------------------------------------------------
// Pipeline I/O
// -------------------------------------------------------------------------

export interface SalesPipelineRequest {
  product_name: string;
  value_proposition: string;
  icp: IdealCustomerProfile;
  entry_stage?: PipelineStage;
  max_prospects?: number;
  existing_prospects?: Prospect[];
  company_context?: string;
  case_study_snippets?: string[];
}

export interface SalesPipelineResult {
  job_id: string;
  entry_stage: PipelineStage;
  product_name: string;
  prospects: Prospect[];
  outreach_sequences: OutreachSequence[];
  qualified_leads: QualificationScore[];
  nurture_sequences: NurtureSequence[];
  discovery_plans: DiscoveryPlan[];
  proposals: SalesProposal[];
  closing_strategies: ClosingStrategy[];
  coaching_report?: PipelineCoachingReport;
  summary: string;
}

// -------------------------------------------------------------------------
// Job management
// -------------------------------------------------------------------------

export interface SalesPipelineRunResponse {
  job_id: string;
  status: string;
  message: string;
}

export interface SalesPipelineStatusResponse {
  job_id: string;
  status: string;
  current_stage: string;
  progress: number;
  product_name: string;
  last_updated_at: string;
  eta_hint?: string;
  error?: string;
  result?: SalesPipelineResult;
}

export interface SalesPipelineJobListItem {
  job_id: string;
  status: string;
  current_stage: string;
  progress: number;
  product_name: string;
  created_at?: string;
  last_updated_at?: string;
}

// -------------------------------------------------------------------------
// Outcomes
// -------------------------------------------------------------------------

export interface StageOutcome {
  outcome_id: string;
  recorded_at: string;
  pipeline_job_id?: string;
  company_name: string;
  industry?: string;
  stage: PipelineStage;
  outcome: OutcomeResult;
  icp_match_score?: number;
  qualification_score?: number;
  email_touch_number?: number;
  subject_line_used?: string;
  objection_text?: string;
  close_technique_used?: CloseType;
  notes: string;
}

export interface DealOutcome {
  outcome_id: string;
  recorded_at: string;
  pipeline_job_id?: string;
  company_name: string;
  industry?: string;
  deal_size_usd?: number;
  final_stage_reached: PipelineStage;
  result: OutcomeResult;
  loss_reason?: string;
  win_factor?: string;
  close_technique_used?: CloseType;
  objections_raised: string[];
  stages_completed: PipelineStage[];
  icp_match_score?: number;
  qualification_score?: number;
  sales_cycle_days?: number;
  notes: string;
}

export interface RecordStageOutcomeRequest {
  company_name: string;
  stage: PipelineStage;
  outcome: OutcomeResult;
  pipeline_job_id?: string;
  industry?: string;
  icp_match_score?: number;
  qualification_score?: number;
  email_touch_number?: number;
  subject_line_used?: string;
  objection_text?: string;
  close_technique_used?: CloseType;
  notes?: string;
}

export interface RecordDealOutcomeRequest {
  company_name: string;
  result: OutcomeResult;
  final_stage_reached: PipelineStage;
  pipeline_job_id?: string;
  industry?: string;
  deal_size_usd?: number;
  loss_reason?: string;
  win_factor?: string;
  close_technique_used?: CloseType;
  objections_raised?: string[];
  stages_completed?: PipelineStage[];
  icp_match_score?: number;
  qualification_score?: number;
  sales_cycle_days?: number;
  notes?: string;
}

export interface RecordOutcomeResponse {
  outcome_id: string;
  message: string;
}

// -------------------------------------------------------------------------
// Learning Insights
// -------------------------------------------------------------------------

export interface LearningInsights {
  total_outcomes_analyzed: number;
  win_rate: number;
  stage_conversion_rates: Record<string, number>;
  top_performing_industries: string[];
  top_icp_signals: string[];
  best_outreach_angles: string[];
  common_objections: string[];
  best_close_techniques: string[];
  winning_patterns: string[];
  losing_patterns: string[];
  avg_deal_size_won_usd?: number;
  avg_sales_cycle_days?: number;
  actionable_recommendations: string[];
  generated_at: string;
  insights_version: number;
}

export interface InsightsRefreshResponse {
  message: string;
  insights_version: number;
  total_outcomes_analyzed: number;
  win_rate: number;
}

export interface OutcomeSummary {
  stage_outcomes: number;
  deal_outcomes: number;
  has_insights: boolean;
}

// -------------------------------------------------------------------------
// Health
// -------------------------------------------------------------------------

export interface SalesHealthResponse {
  status: string;
  strands_sdk: string;
  stage_outcomes_recorded: string;
  deal_outcomes_recorded: string;
  insights_available: string;
}
