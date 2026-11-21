import type {
  AuditEvent,
  CustomerContext,
  Execution,
  Overview,
  PolicyCatalog,
  PolicyDecision,
  ProbeResult,
  PulseAction,
  PulseActor,
  PulseStatus,
  RunDetail,
  RunSummary,
  Scenario,
  SecurityEvent,
  Signal,
  WorkflowInfo,
} from "@/types/pulse";

export class PulseError extends Error {
  constructor(
    message: string,
    public status: number,
    public code?: string,
  ) {
    super(message);
  }
}

// Same-origin through the Next rewrite (/api/sales/* → backend /api/*), so the
// httpOnly session cookie is sent automatically. No credential lives in JS.
async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/sales/pulse/${path}`, {
      ...init,
      credentials: "same-origin",
      headers: {
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...(init?.method && init.method !== "GET" ? { "X-Pulse-Request": "1" } : {}),
        ...init?.headers,
      },
    });
  } catch {
    throw new PulseError("The SignalPilot API is not reachable. Is the backend running on port 8001?", 0);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : typeof detail?.message === "string"
          ? detail.message
          : `Request failed (HTTP ${response.status})`;
    throw new PulseError(message, response.status, detail?.code);
  }
  return response.json() as Promise<T>;
}

const post = <T>(path: string, body?: unknown) =>
  call<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) });

export const pulseApi = {
  status: () => call<PulseStatus>("status"),
  me: () => call<{ actor: PulseActor | null; auth_mode: string }>("auth/me"),
  users: () => call<{ auth_mode: string; users: PulseActor[] }>("auth/users"),
  login: (user_id: string, password?: string) =>
    post<{ actor: PulseActor }>("auth/login", { user_id, password }),
  logout: () => post<{ ok: boolean }>("auth/logout"),
  overview: () => call<Overview>("overview"),
  signals: (limit = 30) =>
    call<{ items: Signal[]; basis: string; mode: string; snapshot_id: string }>(`signals?limit=${limit}`),
  startRun: (customer_id: string, scenario: Scenario, objective?: string) =>
    post<{ run_id: string }>("runs", { customer_id, scenario, ...(objective ? { objective } : {}) }),
  runs: (limit = 30) => call<{ items: RunSummary[] }>(`runs?limit=${limit}`),
  run: (id: string) => call<RunDetail>(`runs/${encodeURIComponent(id)}`),
  actions: (status?: string) =>
    call<{ items: PulseAction[] }>(`actions${status ? `?status=${status}` : ""}`),
  approve: (id: string, payload_hash: string, comment: string) =>
    post<{ status: string; resumed: boolean }>(`actions/${id}/approve`, { payload_hash, comment }),
  reject: (id: string, comment: string) =>
    post<{ status: string; resumed: boolean }>(`actions/${id}/reject`, { comment }),
  executions: () => call<{ items: Execution[] }>("executions"),
  retry: (id: string) => post<{ status: string; error?: string }>(`executions/${id}/retry`),
  workflows: () => call<WorkflowInfo>("workflows"),
  triggerScan: () => post<{ n8n_status: number; result: unknown }>("workflows/scan"),
  audit: (params: { run_id?: string; kind?: string; before_id?: number; limit?: number } = {}) => {
    const q = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== "" && q.set(k, String(v)));
    return call<{
      items: AuditEvent[];
      chain: { valid: boolean; events: number; broken_at: number | null };
      integrity_note: string;
    }>(`audit?${q}`);
  },
  decisions: () => call<{ items: PolicyDecision[] }>("security/decisions"),
  securityEvents: () => call<{ items: SecurityEvent[] }>("security/events"),
  policy: () => call<PolicyCatalog>("security/policy"),
  probe: (name: string) => post<ProbeResult>(`security/probes/${name}`),
  customerContext: (id: string) => call<CustomerContext>(`customers/${encodeURIComponent(id)}/context`),
};
