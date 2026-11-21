// Contracts for /api/pulse (see backend/app/pulse/api.py).

export type Role = "viewer" | "analyst" | "sales_manager" | "director";
export interface PulseActor {
  id: string;
  name: string;
  role: Role | string;
  kind: string;
  permissions: string[];
}

export interface PulseStatus {
  snapshot_id: string;
  demo_mode: boolean;
  auth_mode: "demo" | "password";
  schema_version: number;
  gemini: { configured: boolean; model: string; missing_env: string | null };
  n8n: {
    reachable: boolean;
    url: string;
    status_code?: number;
    error?: string;
    webhook_secret_configured: boolean;
  };
  email: { external_delivery: boolean; mode: string };
  approval_ttl_seconds: number;
  auto_approve_low_risk: boolean;
  scenarios: Scenario[];
}

export type Scenario =
  | "standard"
  | "gemini_unavailable"
  | "n8n_unavailable"
  | "short_expiry"
  | "prompt_injection";

export type Severity = "critical" | "high" | "medium" | "opportunity";

export interface Signal {
  customer_id: string;
  display_name: string;
  industry: string | null;
  value_tier: string;
  value_percentile: number;
  volume_12m: number;
  avg_last_3m: number;
  avg_prior_12m: number;
  change_pct: number | null;
  months_silent: number | null;
  key: string;
  severity: Severity;
  title: string;
  fact: string;
  latest_run?: { id: string; status: string; created_at: string } | null;
}

export interface RunSummary {
  id: string;
  objective: string;
  customer_id: string;
  snapshot_id: string;
  started_by: string;
  scenario: string | null;
  status: string;
  stage: string | null;
  llm_source: string | null;
  summary: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  action_counts?: Record<string, number>;
}

export interface ToolCall {
  tool: string;
  args: Record<string, unknown>;
  ok: boolean;
  ms: number | null;
  result_summary?: string;
  error?: string;
}

export interface AgentStep {
  id: number;
  agent: string;
  status: string;
  attempt: number;
  input_summary: string | null;
  output: unknown;
  tool_calls: ToolCall[] | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
}

export interface PolicyDecision {
  id: number;
  action_id: string | null;
  run_id: string | null;
  phase: string;
  actor: string;
  action_type: string;
  outcome: "allow" | "require_approval" | "deny";
  risk: string | null;
  rules: string[];
  reasons: string[];
  payload_hash: string | null;
  created_at: string;
}

export interface Execution {
  id: string;
  action_id: string;
  idempotency_key: string;
  status: string;
  attempts: number;
  n8n_execution_id: string | null;
  destination: string | null;
  result: Record<string, unknown> | null;
  error: string | null;
  submitted_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  action_type?: string;
  customer_id?: string;
  run_id?: string;
}

export interface PulseAction {
  id: string;
  run_id: string;
  customer_id: string;
  action_type: string;
  label: string;
  external: boolean | null;
  payload: Record<string, unknown>;
  payload_hash: string;
  requested_by: string;
  rationale: string | null;
  status: string;
  risk: string | null;
  required_role: string | null;
  policy_outcome: {
    outcome: string;
    rules: string[];
    reasons: string[];
    approval_permission?: string | null;
    annotations?: Record<string, unknown>;
  };
  expires_at: string | null;
  created_at: string;
  approval: {
    decision: string;
    approver: string;
    approver_role: string;
    comment: string | null;
    decided_at: string;
    expires_at: string;
    consumed_at: string | null;
  } | null;
  execution: Execution | null;
  decisions: PolicyDecision[];
  run?: Pick<RunSummary, "id" | "customer_id" | "summary" | "llm_source" | "started_by"> | null;
  can_decide?: boolean;
}

export interface RunDetail {
  run: RunSummary;
  steps: AgentStep[];
  actions: PulseAction[];
}

export interface AuditEvent {
  id: number;
  at: string;
  kind: string;
  actor: string;
  run_id: string | null;
  action_id: string | null;
  execution_id: string | null;
  summary: string;
  detail: Record<string, unknown>;
  hash: string;
  prev_hash: string;
}

export interface Overview {
  snapshot_id: string;
  signals: {
    critical: number;
    high: number;
    medium: number;
    opportunity: number;
    volume_at_risk_12m: number;
    volume_basis: string;
  };
  runs: Record<string, number>;
  actions: Record<string, number>;
  executions: Record<string, number>;
  policy_outcomes: Record<string, number>;
  pending_approvals: number;
  recent_events: AuditEvent[];
  recent_runs: RunSummary[];
}

export interface CustomerContext {
  profile: { customer_id: string; display_name: string; industry: string | null };
  activity: {
    volume_12m: number;
    avg_last_3m: number;
    avg_prior_12m: number;
    change_pct: number | null;
    months_silent: number | null;
    series_last_15m: { month: string; calibrations: number }[];
    signals: { key: string; severity: Severity; title: string; fact: string }[];
  };
  due_requirements: { overdue: number; upcoming: number; reference_date: string };
  crm: {
    synthetic: boolean;
    has_verified_contact: boolean;
    contact_name: string | null;
    account_owner: string;
    notes: { date: string; author: string; text: string }[];
  };
  peers: { industry: string | null; peer_median_12m: number | null; customer_12m: number };
}

export interface SecurityEvent {
  id: number;
  kind: string;
  severity: string;
  actor: string | null;
  subject: string | null;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface PolicyCatalog {
  actions: {
    action_type: string;
    label: string;
    risk: string;
    required_role: string;
    approval: string;
    external: boolean;
    destination: string;
  }[];
  default: string;
  proposers: string[];
  limits: { max_discount_pct: number; max_discount_months: number; approval_ttl_seconds: number };
  roles: Record<string, string[]>;
}

export interface WorkflowInfo {
  n8n: { reachable: boolean; url: string; error?: string };
  execute_webhook: string;
  callback_url: string;
  workflows: {
    id: string;
    name: string;
    trigger: string;
    purpose: string;
    handlers?: Record<string, string>;
  }[];
  execution_stats: Record<string, number>;
  recent_failures: (Execution & { action_type: string; customer_id: string })[];
}

export interface ProbeResult {
  probe: string;
  run_id: string;
  customer_id: string;
  expected: string;
  observed: unknown;
  blocked: boolean | null;
  rules?: string[];
  reasons?: string[];
}
