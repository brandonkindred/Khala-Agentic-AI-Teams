/**
 * Models for the Investment Team API.
 */

// ---------------------------------------------------------------------------
// Enums and Constants
// ---------------------------------------------------------------------------

export type RiskTolerance = 'low' | 'medium' | 'high' | 'very_high';

export type WorkflowMode = 'advisory' | 'paper' | 'live' | 'monitor_only';

export type PromotionStage = 'reject' | 'revise' | 'paper' | 'live';

export type ValidationStatus = 'pass' | 'warn' | 'fail';

export type PromotionGate =
  | 'separation_of_duties'
  | 'risk_veto'
  | 'validation'
  | 'ips_permission'
  | 'human_approval';

export type GateResult = 'pass' | 'fail' | 'warn';

export const RISK_TOLERANCE_OPTIONS: { value: RiskTolerance; label: string }[] = [
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
  { value: 'very_high', label: 'Very High' },
];

export const WORKFLOW_MODE_OPTIONS: { value: WorkflowMode; label: string; description: string }[] = [
  { value: 'advisory', label: 'Advisory', description: 'Recommendations only, no execution' },
  { value: 'paper', label: 'Paper', description: 'Simulated trading for validation' },
  { value: 'live', label: 'Live', description: 'Real trading execution' },
  { value: 'monitor_only', label: 'Monitor Only', description: 'Passive monitoring mode' },
];

export const QUEUE_NAMES = [
  'research',
  'portfolio_design',
  'validation',
  'promotion',
  'execution',
  'escalation',
] as const;

export type QueueName = typeof QUEUE_NAMES[number];

// ---------------------------------------------------------------------------
// Core Profile Models
// ---------------------------------------------------------------------------

export interface PlannedLargeExpense {
  name: string;
  amount: number;
  date: string;
}

export interface LiquidityNeeds {
  emergency_fund_months: number;
  planned_large_expenses: PlannedLargeExpense[];
}

export interface IncomeProfile {
  annual_gross: number;
  stability: string;
}

export interface NetWorth {
  total: number;
  investable_assets: number;
}

export interface SavingsRate {
  monthly: number;
  annual: number;
}

export interface TaxProfile {
  country: string;
  state: string;
  account_types: string[];
}

export interface UserPreferences {
  excluded_asset_classes: string[];
  excluded_industries: string[];
  esg_preference: string;
  crypto_allowed: boolean;
  options_allowed: boolean;
  leverage_allowed: boolean;
}

export interface UserGoal {
  name: string;
  target_amount: number;
  target_date: string;
  priority: string;
}

export interface PortfolioConstraints {
  max_single_position_pct: number;
  max_asset_class_pct: Record<string, number>;
}

export interface InvestmentProfile {
  schema_version: string;
  user_id: string;
  created_at: string;
  risk_tolerance: RiskTolerance;
  max_drawdown_tolerance_pct: number;
  time_horizon_years: number;
  liquidity_needs: LiquidityNeeds;
  income: IncomeProfile;
  net_worth: NetWorth;
  savings_rate: SavingsRate;
  tax_profile: TaxProfile;
  preferences: UserPreferences;
  goals: UserGoal[];
  constraints: PortfolioConstraints;
}

export interface IPS {
  profile: InvestmentProfile;
  live_trading_enabled: boolean;
  human_approval_required_for_live: boolean;
  speculative_sleeve_cap_pct: number;
  rebalance_frequency: string;
  default_mode: WorkflowMode;
  notes: string[];
}

// ---------------------------------------------------------------------------
// Portfolio Models
// ---------------------------------------------------------------------------

export interface AuditContext {
  data_snapshot_id: string;
  assumptions: string[];
  calc_artifacts: string[];
  gate_trace: string[];
  agent_versions: Record<string, string>;
}

export interface PortfolioPosition {
  symbol: string;
  asset_class: string;
  weight_pct: number;
  rationale: string;
}

export interface PortfolioProposal {
  proposal_id: string;
  prepared_by: string;
  ips_version: string;
  data_snapshot_id: string;
  objective: string;
  positions: PortfolioPosition[];
  expected_return_pct?: number;
  expected_volatility_pct?: number;
  expected_max_drawdown_pct?: number;
  assumptions: string[];
  audit: AuditContext;
}

// ---------------------------------------------------------------------------
// Strategy Models
// ---------------------------------------------------------------------------

export interface StrategySpec {
  strategy_id: string;
  authored_by: string;
  asset_class: string;
  hypothesis: string;
  signal_definition: string;
  entry_rules: string[];
  exit_rules: string[];
  sizing_rules: string[];
  risk_limits: Record<string, unknown>;
  speculative: boolean;
  audit: AuditContext;
}

export interface ValidationCheck {
  name: string;
  status: ValidationStatus;
  details: string;
}

export interface ValidationReport {
  strategy_id: string;
  generated_by: string;
  data_snapshot_id: string;
  backtest_period: string;
  scenario_set: string[];
  checks: ValidationCheck[];
  summary: string;
  audit: AuditContext;
}

// ---------------------------------------------------------------------------
// Promotion Models
// ---------------------------------------------------------------------------

export interface GateCheckResult {
  gate: PromotionGate;
  result: GateResult;
  details: string;
}

export interface PromotionDecision {
  strategy_id: string;
  decided_by: string;
  outcome: PromotionStage;
  rationale: string;
  required_actions: string[];
  gate_results: GateCheckResult[];
  audit: AuditContext;
}

// ---------------------------------------------------------------------------
// Workflow Models
// ---------------------------------------------------------------------------

export interface QueueItem {
  queue: string;
  payload_id: string;
  priority: string;
}

export interface WorkflowState {
  mode: WorkflowMode;
  audit_log: string[];
  queue_counts: Record<string, number>;
}

export interface QueuesState {
  queues: Record<string, QueueItem[]>;
}

// ---------------------------------------------------------------------------
// Committee Memo
// ---------------------------------------------------------------------------

export interface InvestmentCommitteeMemo {
  memo_id: string;
  prepared_for_user_id: string;
  recommendation: string;
  rationale: string;
  dissenting_views: string[];
  attachments: string[];
  audit: AuditContext;
}

// ---------------------------------------------------------------------------
// Request Models
// ---------------------------------------------------------------------------

export interface CreateProfileRequest {
  user_id: string;
  risk_tolerance: RiskTolerance;
  max_drawdown_tolerance_pct: number;
  time_horizon_years: number;
  annual_gross_income: number;
  income_stability?: string;
  total_net_worth: number;
  investable_assets: number;
  monthly_savings?: number;
  annual_savings?: number;
  tax_country?: string;
  tax_state?: string;
  account_types?: string[];
  emergency_fund_months?: number;
  excluded_asset_classes?: string[];
  excluded_industries?: string[];
  esg_preference?: string;
  crypto_allowed?: boolean;
  options_allowed?: boolean;
  leverage_allowed?: boolean;
  goals?: UserGoal[];
  max_single_position_pct?: number;
  max_asset_class_pct?: Record<string, number>;
  live_trading_enabled?: boolean;
  human_approval_required_for_live?: boolean;
  speculative_sleeve_cap_pct?: number;
  rebalance_frequency?: string;
  default_mode?: WorkflowMode;
  notes?: string[];
}

export interface CreateProposalRequest {
  prepared_by: string;
  user_id: string;
  objective: string;
  positions: Partial<PortfolioPosition>[];
  expected_return_pct?: number;
  expected_volatility_pct?: number;
  expected_max_drawdown_pct?: number;
  assumptions?: string[];
}

export interface ValidateProposalRequest {
  user_id: string;
}

export interface CreateStrategyRequest {
  authored_by: string;
  asset_class: string;
  hypothesis: string;
  signal_definition: string;
  entry_rules?: string[];
  exit_rules?: string[];
  sizing_rules?: string[];
  risk_limits?: Record<string, unknown>;
  speculative?: boolean;
}

export interface ValidateStrategyRequest {
  backtest_period?: string;
  scenario_set?: string[];
  checks?: Partial<ValidationCheck>[];
}

export interface PromotionDecisionRequest {
  strategy_id: string;
  user_id: string;
  proposer_agent_id: string;
  approver_agent_id: string;
  approver_role?: string;
  approver_version?: string;
  risk_veto?: boolean;
  human_live_approval?: boolean;
}

export interface CreateMemoRequest {
  user_id: string;
  recommendation: string;
  rationale: string;
  dissenting_views?: string[];
}

// ---------------------------------------------------------------------------
// Response Models
// ---------------------------------------------------------------------------

export interface CreateProfileResponse {
  user_id: string;
  ips: IPS;
  message: string;
}

export interface GetProfileResponse {
  user_id: string;
  ips?: IPS;
  found: boolean;
}

export interface CreateProposalResponse {
  proposal_id: string;
  proposal: PortfolioProposal;
  message: string;
}

export interface GetProposalResponse {
  proposal_id: string;
  proposal?: PortfolioProposal;
  found: boolean;
}

export interface ValidateProposalResponse {
  proposal_id: string;
  valid: boolean;
  violations: string[];
}

export interface CreateStrategyResponse {
  strategy_id: string;
  strategy: StrategySpec;
  message: string;
}

export interface ValidateStrategyResponse {
  strategy_id: string;
  validation: ValidationReport;
  passed: boolean;
  failures: string[];
}

export interface PromotionDecisionResponse {
  strategy_id: string;
  decision: PromotionDecision;
}

export interface WorkflowStatusResponse {
  mode: WorkflowMode;
  audit_log: string[];
  queue_counts: Record<string, number>;
}

export interface QueuesResponse {
  queues: Record<string, QueueItem[]>;
}

export interface CreateMemoResponse {
  memo: InvestmentCommitteeMemo;
}

export interface InvestmentHealthResponse {
  status: string;
  timestamp?: string;
}

// ---------------------------------------------------------------------------
// Backtest Models
// ---------------------------------------------------------------------------

export interface BacktestConfig {
  start_date: string;
  end_date: string;
  initial_capital: number;
  benchmark_symbol: string;
  rebalance_frequency: string;
  transaction_cost_bps: number;
  slippage_bps: number;
}

export interface BacktestResult {
  total_return_pct: number;
  annualized_return_pct: number;
  volatility_pct: number;
  sharpe_ratio: number;
  max_drawdown_pct: number;
  win_rate_pct: number;
  profit_factor: number;
}

export interface TradeRecord {
  trade_num: number;
  entry_date: string;
  exit_date: string;
  symbol: string;
  side: string;
  entry_price: number;
  exit_price: number;
  shares: number;
  position_value: number;
  gross_pnl: number;
  net_pnl: number;
  return_pct: number;
  hold_days: number;
  outcome: 'win' | 'loss';
  cumulative_pnl: number;
}

export interface BacktestRecord {
  backtest_id: string;
  strategy_id: string;
  strategy: StrategySpec;
  config: BacktestConfig;
  submitted_by: string;
  submitted_at: string;
  completed_at: string;
  status: string;
  result: BacktestResult;
  notes: string[];
  trades: TradeRecord[];
}

// ---------------------------------------------------------------------------
// Strategy Lab Models
// ---------------------------------------------------------------------------

export interface StrategyLabRecord {
  lab_record_id: string;
  strategy: StrategySpec;
  backtest: BacktestRecord;
  is_winning: boolean;
  strategy_rationale: string;
  analysis_narrative: string;
  created_at: string;
}

export interface RunStrategyLabRequest {
  start_date?: string;
  end_date?: string;
  initial_capital?: number;
  benchmark_symbol?: string;
  transaction_cost_bps?: number;
  slippage_bps?: number;
}

export interface StrategyLabRunResponse {
  record: StrategyLabRecord;
  message: string;
}

export interface StrategyLabResultsResponse {
  items: StrategyLabRecord[];
  count: number;
  winning_count: number;
  losing_count: number;
}

// ---------------------------------------------------------------------------
// Financial Advisor (Chat) Models
// ---------------------------------------------------------------------------

export interface StartAdvisorSessionRequest {
  user_id: string;
}

export interface SendAdvisorMessageRequest {
  message: string;
}

export interface AdvisorSessionResponse {
  session_id: string;
  advisor_message: string;
  session_status: 'active' | 'completed' | 'awaiting_confirmation';
  current_topic?: string;
  missing_fields?: string[];
}

export interface AdvisorSessionStateResponse {
  session_id: string;
  session_status: 'active' | 'completed' | 'awaiting_confirmation';
  current_topic?: string;
  missing_fields?: string[];
  messages: AdvisorChatMessage[];
}

export interface AdvisorChatMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
}

export interface CompleteAdvisorSessionResponse {
  session_id: string;
  ips: IPS;
  message: string;
}
