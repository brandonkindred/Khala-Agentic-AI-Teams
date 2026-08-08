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
// Strategy DSL (mirrors backend/agents/investment_team/strategy_lab/spec_dsl.py).
// Wire-format types: IndicatorRef has the same { name, params, source } shape
// as the Python Pydantic class. Predicate sides accept either a structured
// IndicatorRef, a bar-field literal string, or (rhs only) a numeric constant.
// ---------------------------------------------------------------------------

export type IndicatorSource = 'close' | 'high' | 'low' | 'open' | 'volume' | 'hl2' | 'ohlc4';

export type ComparisonOp = '<' | '>' | '<=' | '>=' | '==' | 'cross_above' | 'cross_below';

export const COMPARISON_OP_OPTIONS: { value: ComparisonOp; label: string }[] = [
  { value: '<', label: '<' },
  { value: '>', label: '>' },
  { value: '<=', label: '<=' },
  { value: '>=', label: '>=' },
  { value: '==', label: '==' },
  { value: 'cross_above', label: 'crosses above' },
  { value: 'cross_below', label: 'crosses below' },
];

export type IndicatorName =
  | 'sma'
  | 'ema'
  | 'rsi'
  | 'macd'
  | 'bollinger'
  | 'atr'
  | 'adx'
  | 'stochastic'
  | 'vwap'
  | 'donchian'
  | 'keltner'
  | 'obv'
  | 'mfi'
  | 'roc'
  | 'cci'
  | 'williams_r';

export const INDICATOR_NAME_OPTIONS: IndicatorName[] = [
  'sma', 'ema', 'rsi', 'macd', 'bollinger', 'atr', 'adx', 'stochastic', 'vwap',
  'donchian', 'keltner', 'obv', 'mfi', 'roc', 'cci', 'williams_r',
];

export type IndicatorParamValue = number | string;

export interface IndicatorRef {
  name: IndicatorName;
  params: Record<string, IndicatorParamValue>;
  source?: IndicatorSource;
}

export type BarFieldRef = 'bar.close' | 'bar.high' | 'bar.low' | 'bar.volume';

export const BAR_FIELD_OPTIONS: BarFieldRef[] = ['bar.close', 'bar.high', 'bar.low', 'bar.volume'];

export const INDICATOR_SOURCE_OPTIONS: IndicatorSource[] = [
  'close', 'high', 'low', 'open', 'volume', 'hl2', 'ohlc4',
];

// Mirrors _INDICATOR_PARAM_SPECS in spec_dsl.py — kept in sync because the form
// needs to know per-indicator which params to render, their bounds, and whether
// the source override is accepted.
export interface IndicatorParamSpec {
  key: string;
  required: boolean;
  default?: number | string;
  kind: 'int' | 'float' | 'enum';
  min?: number;
  max?: number;
  options?: string[];
}

export interface IndicatorSpec {
  name: IndicatorName;
  allowSource: boolean;
  params: IndicatorParamSpec[];
}

export const INDICATOR_SPECS: Record<IndicatorName, IndicatorSpec> = {
  sma: {
    name: 'sma', allowSource: true,
    params: [{ key: 'period', required: true, kind: 'int', min: 2, max: 400 }],
  },
  ema: {
    name: 'ema', allowSource: true,
    params: [{ key: 'period', required: true, kind: 'int', min: 2, max: 400 }],
  },
  rsi: {
    name: 'rsi', allowSource: true,
    params: [{ key: 'period', required: false, default: 14, kind: 'int', min: 2, max: 200 }],
  },
  macd: {
    name: 'macd', allowSource: true,
    params: [
      { key: 'fast', required: false, default: 12, kind: 'int', min: 2, max: 200 },
      { key: 'slow', required: false, default: 26, kind: 'int', min: 3, max: 400 },
      { key: 'signal', required: false, default: 9, kind: 'int', min: 2, max: 100 },
      { key: 'output', required: false, default: 'macd', kind: 'enum', options: ['macd', 'signal', 'histogram'] },
    ],
  },
  bollinger: {
    name: 'bollinger', allowSource: true,
    params: [
      { key: 'period', required: false, default: 20, kind: 'int', min: 5, max: 200 },
      { key: 'num_std', required: false, default: 2.0, kind: 'float', min: 0.000001 },
      { key: 'band', required: false, default: 'middle', kind: 'enum', options: ['upper', 'middle', 'lower', 'percent_b', 'bandwidth'] },
    ],
  },
  atr: {
    name: 'atr', allowSource: false,
    params: [{ key: 'period', required: false, default: 14, kind: 'int', min: 2, max: 200 }],
  },
  adx: {
    name: 'adx', allowSource: false,
    params: [{ key: 'period', required: false, default: 14, kind: 'int', min: 2, max: 200 }],
  },
  stochastic: {
    name: 'stochastic', allowSource: false,
    params: [
      { key: 'k_period', required: false, default: 14, kind: 'int', min: 2, max: 200 },
      { key: 'd_period', required: false, default: 3, kind: 'int', min: 1, max: 100 },
      { key: 'output', required: false, default: 'k', kind: 'enum', options: ['k', 'd'] },
    ],
  },
  vwap: {
    name: 'vwap', allowSource: false,
    params: [{ key: 'period', required: false, default: 20, kind: 'int', min: 2, max: 400 }],
  },
  donchian: {
    name: 'donchian', allowSource: false,
    params: [
      { key: 'period', required: false, default: 20, kind: 'int', min: 2, max: 400 },
      { key: 'band', required: false, default: 'middle', kind: 'enum', options: ['upper', 'middle', 'lower'] },
    ],
  },
  keltner: {
    name: 'keltner', allowSource: false,
    params: [
      { key: 'period', required: false, default: 20, kind: 'int', min: 2, max: 400 },
      { key: 'atr_period', required: false, default: 10, kind: 'int', min: 2, max: 200 },
      { key: 'multiplier', required: false, default: 2.0, kind: 'float', min: 0.000001 },
      { key: 'band', required: false, default: 'middle', kind: 'enum', options: ['upper', 'middle', 'lower'] },
    ],
  },
  obv: { name: 'obv', allowSource: false, params: [] },
  mfi: {
    name: 'mfi', allowSource: false,
    params: [{ key: 'period', required: false, default: 14, kind: 'int', min: 2, max: 200 }],
  },
  roc: {
    name: 'roc', allowSource: true,
    params: [{ key: 'period', required: false, default: 12, kind: 'int', min: 2, max: 400 }],
  },
  cci: {
    name: 'cci', allowSource: false,
    params: [{ key: 'period', required: false, default: 20, kind: 'int', min: 2, max: 400 }],
  },
  williams_r: {
    name: 'williams_r', allowSource: false,
    params: [{ key: 'period', required: false, default: 14, kind: 'int', min: 2, max: 200 }],
  },
};

export type PredicateSide = IndicatorRef | BarFieldRef;

export interface Predicate {
  lhs: PredicateSide;
  op: ComparisonOp;
  rhs: PredicateSide | number;
}

export interface EntryRule {
  kind: 'entry';
  side: 'long' | 'short';
  when: Predicate;
  note?: string;
}

export interface StopLossRule    { kind: 'stop_loss'; pct: number; basis?: 'entry_price' | 'trailing_high' | 'trailing_low'; note?: string; }
export interface TakeProfitRule  { kind: 'take_profit'; pct: number; note?: string; }
export interface SignalExitRule  { kind: 'signal_exit'; when: Predicate; note?: string; }

// Exit rules: structured close conditions the engine enforces (stop-loss /
// take-profit) plus aspirational signal-based exits. Bar-counting "time
// stops" are deliberately NOT a member — real traders close on price,
// P&L, or signal reversal.
export type ExitRule = StopLossRule | TakeProfitRule | SignalExitRule;

export const EXIT_RULE_KINDS = ['stop_loss', 'take_profit', 'signal_exit'] as const;
export type ExitRuleKind = typeof EXIT_RULE_KINDS[number];

export type StopLossBasis = 'entry_price' | 'trailing_high' | 'trailing_low';

export const STOP_LOSS_BASIS_OPTIONS: StopLossBasis[] = [
  'entry_price', 'trailing_high', 'trailing_low',
];

export interface FixedFractionSizing    { kind: 'fixed_fraction'; fraction: number; note?: string; }
export interface VolatilityTargetSizing { kind: 'volatility_target'; target_annual_vol: number; note?: string; }
export interface FixedNotionalSizing    { kind: 'fixed_notional'; notional_usd: number; note?: string; }

export type SizingRule = FixedFractionSizing | VolatilityTargetSizing | FixedNotionalSizing;

export const SIZING_KINDS = ['fixed_fraction', 'volatility_target', 'fixed_notional'] as const;
export type SizingKind = typeof SIZING_KINDS[number];

export type StrategyTimeframe = '1m' | '5m' | '15m' | '1h' | '1d';

export const STRATEGY_TIMEFRAME_OPTIONS: StrategyTimeframe[] = ['1m', '5m', '15m', '1h', '1d'];

// ---------------------------------------------------------------------------
// Strategy Models
// ---------------------------------------------------------------------------

export interface StrategySpec {
  strategy_id: string;
  authored_by: string;
  asset_class: string;
  hypothesis: string;
  signal_definition: string;
  timeframe: StrategyTimeframe;
  entry_rules: EntryRule[];
  exit_rules: ExitRule[];
  sizing: SizingRule;
  target_symbols: string[];
  risk_limits: Record<string, unknown>;
  speculative: boolean;
  strategy_code?: string;
  requires_redesign: boolean;
  unparsed_rules: string[];
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
  timeframe: StrategyTimeframe;
  entry_rules?: EntryRule[];
  exit_rules?: ExitRule[];
  sizing?: SizingRule;
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

/** Signal Intelligence Expert output or skip metadata from the strategy lab batch run. */
export type SignalIntelligenceBriefPayload = Record<string, unknown> | null;

export interface StrategyLabRecord {
  lab_record_id: string;
  strategy: StrategySpec;
  backtest: BacktestRecord;
  is_winning: boolean;
  /**
   * True when the record clears return threshold + realism/alignment/
   * conformance/lookahead gates. Paper-trading is gated on this flag.
   * Missing on legacy rows — treat as false.
   */
  is_publishable?: boolean;
  /**
   * Comma-joined failing publishability gate codes when winning but not
   * publishable. Null/undefined when publishable or on legacy rows.
   */
  publishability_skip_reason?: string | null;
  /** Integrated paper-trade step status: skipped | completed | failed. */
  paper_trading_status?: string | null;
  /** Skip reason when paper_trading_status === 'skipped'. */
  paper_trading_skipped_reason?: string | null;
  strategy_rationale: string;
  analysis_narrative: string;
  created_at: string;
  refinement_rounds?: number;
  quality_gate_results?: QualityGateResult[];
  strategy_code?: string;
  /** Ideation-time spec before any refinement-driven mutation; null on legacy rows. */
  original_spec?: StrategySpec | null;
  /** Ideation-time strategy code before refinement; null on legacy rows. */
  original_code?: string | null;
  /** Present on new runs: expert JSON or `{ skipped, skipped_reason }`. Legacy rows: undefined/null. */
  signal_intelligence_brief?: SignalIntelligenceBriefPayload;
}

export interface RunStrategyLabRequest {
  start_date?: string;
  end_date?: string;
  initial_capital?: number;
  benchmark_symbol?: string;
  transaction_cost_bps?: number;
  slippage_bps?: number;
  /** Strategies to generate per batch (sequential; default 10, max 25). */
  batch_size?: number;
  /**
   * Number of batches to run back-to-back (default 1). Upper bound is
   * operator-configurable via STRATEGY_LAB_MAX_BATCH_COUNT (default 100);
   * fetch the current limit from GET /strategy-lab/config. Each batch
   * ideates with full context of every strategy from prior batches and
   * refreshes the signal-intelligence brief.
   */
  batch_count?: number;
  /**
   * Asset categories the design agent is allowed to generate strategies for —
   * a subset of: stocks, crypto, forex, futures, commodities. When omitted (or
   * covering every category) the lab may generate for any category. When a
   * strict subset is supplied, ideation is constrained to those categories.
   */
  allowed_asset_classes?: string[];
}

export interface StrategyLabConfigResponse {
  batch_count_min: number;
  batch_count_max: number;
  /**
   * Ideation-valid asset categories the design agent can generate strategies
   * for. The UI sources its category selector from this list so it stays in
   * sync with the backend. May be absent on older servers.
   */
  asset_categories?: string[];
}

export interface StrategyLabRunResponse {
  records: StrategyLabRecord[];
  count: number;
  message: string;
}

export interface StrategyLabResultsResponse {
  items: StrategyLabRecord[];
  count: number;
  winning_count: number;
  losing_count: number;
}

export interface DeleteStrategyLabRecordResponse {
  lab_record_id: string;
  deleted_strategy_id: string | null;
  deleted_backtest_id: string | null;
  deleted_paper_trading_sessions: number;
}

export interface ClearStrategyLabStorageResponse {
  deleted_lab_records: number | null;
  deleted_lab_strategies: number | null;
  deleted_lab_backtests: number | null;
  deleted_paper_trading_sessions: number | null;
  message: string;
}

// Strategy Lab — real-time run tracking

export interface QualityGateResult {
  gate_name: string;
  passed: boolean;
  details: string;
  severity: 'info' | 'warning' | 'critical';
  refinement_round?: number;
}

export interface StrategyLabCycleProgress {
  cycle_index: number;
  /**
   * Not the closed 4-value UI stepper set (`STRATEGY_LAB_PHASES` in
   * phase-stepper.component.ts): the backend's real phase vocabulary is
   * open-ended (`designing`, `aligning`, `telemetry`, paper-trading phases,
   * ...) — same reasoning as `StrategyLabProgressEvent.phase` below, which
   * is this field's sole source.
   */
  phase: string;
  sub_phase?: string;
  refinement_round?: number;
  /**
   * `| null`, not just optional: `_run_state_to_response(...)
   * .model_dump(mode="json")` (backend) serializes an unset Optional[...]
   * as JSON `null`, and both the REST run-status poll and the SSE snapshot
   * event share that one serializer — so a real `null` can reach this field
   * via either transport, not just an omitted key.
   */
  strategy?: { asset_class: string; hypothesis: string } | null;
  metrics?: Partial<BacktestResult> | null;
  checks_passed?: number;
  checks_total?: number;
  symbols_count?: number;
  bars_count?: number;
  trades_count?: number;
  execution_time?: number;
  failure_phase?: string;
  changes_made?: string;
  is_winning?: boolean;
}

export interface StrategyLabErroredDetail {
  cycle_index: number;
  batch_index?: number;
  error: string;
  exception_type?: string;
  reason?: string;
}

export interface StrategyLabRunStatus {
  run_id: string;
  /**
   * Includes `'interrupted'`, a real, tested backend status
   * (`_STRATEGY_LAB_CANCEL_STATUSES` in main.py) that both the REST
   * run-status poll and the SSE snapshot event can report — e.g. after a
   * server restart reclaims an in-flight job via `mark_all_active_jobs_
   * interrupted`. Previously widened only locally on the SSE snapshot
   * event's `status`; merged in here since `_run_state_to_response()`
   * backs both the REST poll and the SSE snapshot with the same
   * unconstrained-string field, so the REST path can report it too.
   */
  status: 'running' | 'completed' | 'completed_with_errors' | 'failed' | 'cancelled' | 'interrupted';
  started_at: string;
  total_cycles: number;
  completed_cycles: number;
  skipped_cycles: number;
  /** Non-fatal per-cycle failures — run kept going but user should see the count. */
  errored_cycles?: number;
  errored_details?: StrategyLabErroredDetail[];
  /**
   * Uncapped count of `errored_details` entries tagged
   * `reason: 'tracker_merge_failed'` (main.py's `tracker_merge_error_count`
   * on `StrategyLabRunStatusResponse`). Unlike `errored_details` itself
   * (capped at 50 entries server-side), this never evicts, so it stays the
   * authoritative source for reconciling a cycle double-counted by a
   * post-completion tracker-merge failure even once older matching entries
   * have aged out of `errored_details`.
   */
  tracker_merge_error_count?: number;
  /** `| null`: see `StrategyLabCycleProgress`'s own field doc for why. */
  current_cycle?: StrategyLabCycleProgress | null;
  completed_record_ids: string[];
  error?: string;
  /** Strategies-per-batch (default 1 for legacy single-batch runs). */
  batch_size?: number;
  /** Number of batches in the run (default 1). */
  batch_count?: number;
  /** Number of batches completed so far. */
  completed_batches?: number;
  /** 1-indexed currently-running batch, or null between batches. */
  current_batch?: number | null;
}

export interface StrategyLabRunStartResponse {
  run_id: string;
  status: string;
  total_cycles: number;
  message: string;
}

export interface ActiveRunsResponse {
  runs: StrategyLabRunStatus[];
}

export interface InvestmentJobSummary {
  job_id: string;
  status: string;
  label: string;
  progress: number;
  current_phase?: string;
  created_at?: string;
}

export interface InvestmentJobsListResponse {
  jobs: InvestmentJobSummary[];
}

// SSE stream events. One interface per real `type` value emitted by
// investment_team/api/main.py's _publish() calls — every field the backend
// actually sends is modeled, not just what handleStreamEvent() reads today,
// EXCEPT `progress`: its payload comes from 50+ on_phase() call sites across
// the backend orchestrator and isn't exhaustively catalogued, so its field
// set below is scoped to what handleStreamEvent() currently consumes. `ts`
// (an ISO timestamp publish() stamps on most, but not all, frames) is
// deliberately omitted — nothing reads it and its presence isn't uniform.

/**
 * Initial/refresh snapshot — wire shape matches the polling endpoint 1:1
 * (`_run_state_to_response(...).model_dump(mode="json")` backs both). Every
 * Optional[...] field on StrategyLabRunStatusResponse was audited against
 * its Python default: completed_cycles/skipped_cycles/errored_cycles/
 * tracker_merge_error_count/batch_size/batch_count/completed_batches all
 * default to a real 0/1, never None, so they're correctly typed as-is.
 * `current_cycle`/`status`/`phase`/nested `strategy`/`metrics` needed widening to admit a real
 * `null` current_cycle, `'interrupted'` status, and an open-ended phase
 * vocabulary — applied on `StrategyLabRunStatus`/`StrategyLabCycleProgress`
 * themselves (not locally here) since the REST poll endpoint shares this
 * exact serializer and so shares the same wire truth.
 *
 * `error` is the one field still widened locally: `Optional[str] = None`
 * (main.py:346) is the same always-present/nullable pattern, but
 * `StrategyLabRunStatus.error` stays `string | undefined` since nothing
 * else in the frontend reads it and no other call site was audited for the
 * same `null`-vs-omitted guarantee. Required (not `?:`) here since
 * model_dump always emits the key (main.py:386: `error=state.get("error")`).
 */
export interface StrategyLabSnapshotEvent extends Omit<StrategyLabRunStatus, 'error'> {
  type: 'snapshot';
  error: string | null;
}

/**
 * Per-cycle progress ping. `cycle_index` and `phase` are the only fields the
 * backend guarantees on every progress event; the rest are sent on some (not
 * all) events depending on which phase/sub_phase is reporting, so they stay
 * optional here. `phase` is `string`: the backend's real phase set
 * (`designing`, `design_review`, `aligning`, `telemetry`, `phase_transition`,
 * paper-trading phases, ...) is open-ended, well beyond the 4-value UI
 * stepper set (`STRATEGY_LAB_PHASES` in phase-stepper.component.ts).
 *
 * Deliberately incomplete: some real phases carry fields not modeled here
 * (`phase_transition`: from_phase/to_phase/spec_hash/code_hash/attempt;
 * `telemetry`: scope/kind + counters). This union intentionally carries no
 * catch-all index signature, so reading them needs a future, explicit field
 * addition rather than an escape hatch — tracked separately from
 * cataloguing the full phase/sub_phase payload matrix.
 */
export interface StrategyLabProgressEvent {
  type: 'progress';
  cycle_index: number;
  phase: string;
  sub_phase?: string;
  refinement_round?: number;
  strategy?: { asset_class: string; hypothesis: string };
  /**
   * Not numeric-only: a cycle's "complete" sub-phase republishes a full
   * BacktestResult.model_dump() here (orchestrator.py:4020-4026), which
   * includes non-numeric fields (terminated_reason, cost_stress_results,
   * execution_diagnostics, ...). `Record<string, unknown>` reflects that
   * honestly rather than asserting a numeric shape most events don't have.
   */
  metrics?: Record<string, unknown>;
  checks_passed?: number;
  checks_total?: number;
  symbols_count?: number;
  bars_count?: number;
  trades_count?: number;
  execution_time?: number;
  failure_phase?: string;
  changes_made?: string;
  is_winning?: boolean;
}

export interface StrategyLabCycleCompleteEvent { type: 'cycle_complete'; cycle_index: number; record_id: string; completed_cycles: number; batch_index: number; }
/** `reason` is an ordinary backend string, not a closed enum (only "no_market_data" occurs today). */
export interface StrategyLabCycleSkippedEvent  { type: 'cycle_skipped'; cycle_index: number; reason: string; batch_index: number; }
/**
 * `reason` is an ordinary backend string: an exception class name for a cycle's
 * own failure, or the fixed marker `"tracker_merge_failed"` for a
 * post-completion tracker-merge failure. `exception_type` is carried ONLY on the
 * tracker-merge variant (the raising class, e.g. `"ValueError"`), so a
 * live-streamed detail can be shaped identically to the persisted/polled one —
 * for a regular failure the class name is already in `reason`.
 */
export interface StrategyLabCycleErroredEvent  { type: 'cycle_errored'; cycle_index: number; batch_index: number; reason: string; error: string; exception_type?: string; }

export interface StrategyLabBatchStartEvent    { type: 'batch_start'; batch_index: number; total_batches: number; batch_size: number; completed_batches: number; }
export interface StrategyLabBatchCompleteEvent { type: 'batch_complete'; batch_index: number; total_batches: number; completed_batches: number; }
/** `reason` is an ordinary backend string, not a closed enum (only "signal_brief_failed" occurs today). */
export interface StrategyLabBatchWarningEvent  { type: 'batch_warning'; batch_index: number; reason: string; }

export interface StrategyLabCompleteEvent {
  type: 'complete';
  message: string;
  status: 'completed' | 'completed_with_errors';
  completed_count: number;
  skipped_count: number;
  errored_count: number;
  errored_details: StrategyLabErroredDetail[];
  completed_batches: number;
  total_batches: number;
}

export interface StrategyLabErrorDetailEvent  { type: 'error'; detail: string; error?: undefined; terminal_status?: 'failed' | 'interrupted'; }
export interface StrategyLabErrorReclaimEvent { type: 'error'; error: string; detail?: undefined; terminal_status?: undefined; }
/**
 * Two mutually-exclusive wire shapes: two strategy-lab call sites always
 * send `detail` (both genuine failure paths — user cancellation is its own
 * `StrategyLabCancelledEvent`, not an `error`); one shared-infra
 * "subscription reclaimed" call site always sends `error` instead. Each
 * branch declares the other field as optional `undefined` (rather than
 * omitting it) so handleStreamEvent()'s existing `event['detail'] as string`
 * read still type-checks uniformly across the union as `string | undefined`,
 * without needing body changes here.
 *
 * `terminal_status` is carried ONLY by the external-stop `detail` publisher
 * (main.py's between-wave "marked externally" branch), which fires for both
 * `'failed'` and `'interrupted'`. Consumers use it to distinguish the two
 * precisely; when it is absent (every genuine in-run failure, and the reclaim
 * shape) they default to `'failed'`. Declared `?: undefined` on the reclaim
 * branch so it reads uniformly across the union like `detail`/`error` above.
 */
export type StrategyLabErrorEvent = StrategyLabErrorDetailEvent | StrategyLabErrorReclaimEvent;

/**
 * Terminal event for a user-initiated cancellation — a distinct `type`
 * rather than a flag bolted onto `error`, mirroring the blogging team's own
 * cancelled-job SSE event (`backend/agents/blogging/api/background.py`'s
 * `_publish_terminal_event(job_id, "cancelled", ...)`), the established
 * pattern for this exact distinction elsewhere in the codebase. main.py's
 * one cancellation call site is the sole publisher of this type.
 */
export interface StrategyLabCancelledEvent { type: 'cancelled'; detail: string; }

/**
 * Sole terminal frame — always exactly `{ type: 'done' }`. The investment-api
 * service's streamRunStatus() forwards it via `subscriber.next(data)` before
 * completing the observable (`data.type === 'done'` only triggers *after*
 * the forward), so handleStreamEvent() does receive one — it just has no
 * branch that matches `'done'`, so the call is a silent no-op today.
 */
export interface StrategyLabDoneEvent { type: 'done'; }

export type StrategyLabStreamEvent =
  | StrategyLabSnapshotEvent
  | StrategyLabProgressEvent
  | StrategyLabCycleCompleteEvent
  | StrategyLabCycleSkippedEvent
  | StrategyLabCycleErroredEvent
  | StrategyLabBatchStartEvent
  | StrategyLabBatchCompleteEvent
  | StrategyLabBatchWarningEvent
  | StrategyLabCompleteEvent
  | StrategyLabErrorEvent
  | StrategyLabCancelledEvent
  | StrategyLabDoneEvent;

// ---------------------------------------------------------------------------
// Paper Trading Models
// ---------------------------------------------------------------------------

/**
 * Mirrors the backend's `PaperTradingStatus` enum
 * (`backend/agents/investment_team/models.py`): `'running'` is the legacy
 * value; `'opening'` | `'warming_up'` | `'live'` are the PR-2 live-mode
 * in-progress states a session steps through before `run_paper_trading`
 * ever writes `'running'` again. All four are non-terminal.
 */
export type PaperTradingStatus = 'opening' | 'warming_up' | 'live' | 'running' | 'completed' | 'failed';

/**
 * Terminal `PaperTradingStatus` values — anything else is still in flight.
 * Mirrors `_ACTIVE_PT_STATES` in `backend/agents/investment_team/api/main.py`,
 * which treats every other status as active.
 */
export const PAPER_TRADING_TERMINAL_STATUSES: ReadonlySet<PaperTradingStatus> = new Set(['completed', 'failed']);

export function isPaperTradingStatusTerminal(status: PaperTradingStatus): boolean {
  return PAPER_TRADING_TERMINAL_STATUSES.has(status);
}

export type PaperTradingVerdict = 'ready_for_live' | 'not_performant';

export interface PaperTradingComparison {
  backtest_win_rate_pct: number;
  paper_win_rate_pct: number;
  backtest_annualized_return_pct: number;
  paper_annualized_return_pct: number;
  backtest_sharpe_ratio: number;
  paper_sharpe_ratio: number;
  backtest_max_drawdown_pct: number;
  paper_max_drawdown_pct: number;
  backtest_profit_factor: number;
  paper_profit_factor: number;
  win_rate_aligned: boolean;
  return_aligned: boolean;
  sharpe_aligned: boolean;
  drawdown_aligned: boolean;
  profit_factor_aligned: boolean;
  overall_aligned: boolean;
}

export interface PaperTradingSession {
  session_id: string;
  lab_record_id: string;
  strategy: StrategySpec;
  status: PaperTradingStatus;
  initial_capital: number;
  current_capital: number;
  trades: TradeRecord[];
  trade_decisions: Record<string, unknown>[];
  result?: BacktestResult;
  comparison?: PaperTradingComparison;
  verdict?: PaperTradingVerdict;
  divergence_analysis?: string;
  symbols_traded: string[];
  data_source: string;
  data_period_start: string;
  data_period_end: string;
  started_at: string;
  completed_at: string;
}

export interface RunPaperTradingRequest {
  lab_record_id: string;
  initial_capital?: number;
  transaction_cost_bps?: number;
  slippage_bps?: number;
  min_trades?: number;
  lookback_days?: number;
  max_evaluations?: number;
}

export interface PaperTradingResponse {
  session: PaperTradingSession;
  message: string;
}

export interface PaperTradingResultsResponse {
  items: PaperTradingSession[];
  count: number;
  ready_for_live_count: number;
  not_performant_count: number;
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
