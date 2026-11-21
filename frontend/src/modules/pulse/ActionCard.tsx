"use client";
import { useState } from "react";
import { Check, X, RotateCw, ShieldCheck, ShieldAlert, ShieldX } from "lucide-react";
import type { PulseAction } from "@/types/pulse";
import { pulseApi } from "./api";
import { usePulseSession } from "./hooks";
import { roleLabel } from "./PulseLayout";
import { Countdown, Disclosure, Hash, JsonView, Pill, StatusPill, TimeAgo } from "./ui";

function PayloadPreview({ action }: { action: PulseAction }) {
  const p = action.payload as Record<string, string | number>;
  if (action.action_type === "send_retention_email")
    return (
      <div className="pp-email">
        <div><span>To</span>{String(p.recipient_email)} <Pill tone="neutral">verified contact on file</Pill></div>
        <div><span>Subject</span>{String(p.subject)}</div>
        <pre>{String(p.body)}</pre>
      </div>
    );
  const labels: Record<string, string> = {
    owner: "Owner", team: "Team", due_date: "Due", review_date: "Review date", note: "Note", priority: "Priority",
    message: "Message", agenda: "Agenda", discount_pct: "Discount %", duration_months: "Months", justification: "Justification",
  };
  return (
    <dl className="pp-kv">
      {Object.entries(p)
        .filter(([k]) => k !== "customer_id")
        .map(([k, v]) => (
          <div key={k}>
            <dt>{labels[k] ?? k}</dt>
            <dd>{typeof v === "object" ? JSON.stringify(v) : String(v)}</dd>
          </div>
        ))}
    </dl>
  );
}

export function PolicyVerdict({ action }: { action: PulseAction }) {
  const o = action.policy_outcome;
  const Icon = o.outcome === "deny" ? ShieldX : o.outcome === "allow" ? ShieldCheck : ShieldAlert;
  return (
    <div className={`pp-verdict ${o.outcome}`}>
      <div className="pp-verdict-head">
        <Icon size={15} aria-hidden />
        <strong>
          PulseGuard: {o.outcome === "deny" ? "blocked" : o.outcome === "allow" ? "allowed (auto-approved)" : `requires ${roleLabel(action.required_role ?? "")} approval`}
        </strong>
      </div>
      <ul>
        {o.reasons.map((r) => (
          <li key={r}>{r}</li>
        ))}
      </ul>
      <Disclosure summary={`${o.rules.length} rule(s) evaluated`}>
        <div className="pp-rules">
          {o.rules.map((r) => (
            <code key={r}>{r}</code>
          ))}
        </div>
      </Disclosure>
    </div>
  );
}

export function ExecutionStatus({ action, onChanged }: { action: PulseAction; onChanged?: () => void }) {
  const session = usePulseSession();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const e = action.execution;
  if (!e) return null;
  const result = e.result as { delivery?: string; preview?: Record<string, unknown>; followup_id?: string; reason?: string } | null;
  return (
    <div className="pp-exec">
      <div className="pp-exec-head">
        <span>n8n execution</span>
        <StatusPill value={e.status} />
        {e.n8n_execution_id && <Pill tone="neutral">n8n #{e.n8n_execution_id}</Pill>}
        {result?.delivery && (
          <Pill tone={result.delivery === "simulated" ? "orange" : "green"} title={String(result.preview?.reason ?? "")}>
            delivery: {result.delivery}
          </Pill>
        )}
        <span className="pp-muted small">attempts {e.attempts}</span>
      </div>
      {result?.followup_id && <p className="pp-muted small">Created follow-up <code>{result.followup_id}</code> in the SignalPilot workspace.</p>}
      {result?.delivery === "simulated" && result.preview && (
        <p className="pp-note small">Simulated: {String(result.preview.reason)} Nothing left the system.</p>
      )}
      {e.error && <p className="pp-note error small">{e.error}</p>}
      {e.status === "dispatch_failed" && (
        <button
          className="pp-btn ghost small"
          disabled={busy || !session.can("execution:retry")}
          title={session.can("execution:retry") ? "Retry with the same idempotency key" : "Requires sales manager or director"}
          onClick={async () => {
            setBusy(true);
            setError(null);
            try {
              await pulseApi.retry(e.id);
              onChanged?.();
            } catch (err) {
              setError((err as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        >
          <RotateCw size={13} /> {busy ? "Retrying…" : "Retry execution"}
        </button>
      )}
      {error && <p className="pp-note error small">{error}</p>}
      {result && (
        <Disclosure summary="Verified callback result">
          <JsonView value={result} />
        </Disclosure>
      )}
    </div>
  );
}

export function ActionCard({
  action,
  onChanged,
  decide = false,
}: {
  action: PulseAction;
  onChanged?: () => void;
  decide?: boolean;
}) {
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pending = action.status === "pending_approval";
  const adversarial = (action.rationale ?? "").startsWith("ADVERSARIAL");
  const act = async (kind: "approve" | "reject") => {
    setBusy(kind);
    setError(null);
    try {
      if (kind === "approve") await pulseApi.approve(action.id, action.payload_hash, comment);
      else await pulseApi.reject(action.id, comment);
      onChanged?.();
    } catch (e) {
      setError((e as Error).message);
      onChanged?.();
    } finally {
      setBusy(null);
    }
  };
  return (
    <article className={`pp-action ${action.status}`} aria-label={`${action.label} for ${action.customer_id}`}>
      <header>
        <div>
          <h3>{action.label}</h3>
          <p className="pp-muted small">
            {action.customer_id} · requested by <code>{action.requested_by}</code> · <TimeAgo iso={action.created_at} />
          </p>
        </div>
        <div className="pp-action-badges">
          {adversarial && <Pill tone="purple">red-team test</Pill>}
          {action.risk && <StatusPill value={action.risk} title="Risk level" />}
          {action.external && <Pill tone="orange">external</Pill>}
          <StatusPill value={action.status} />
          {pending && <Countdown iso={action.expires_at} />}
        </div>
      </header>
      {action.rationale && <p className="pp-rationale">{action.rationale}</p>}
      <PayloadPreview action={action} />
      <p className="pp-muted small">
        Payload hash <Hash value={action.payload_hash} /> — the approval is bound to exactly this content.
      </p>
      <PolicyVerdict action={action} />
      {action.approval && (
        <p className="pp-muted small">
          {action.approval.decision === "approved" ? "Approved" : "Rejected"} by <strong>{action.approval.approver}</strong> (
          {roleLabel(action.approval.approver_role)}) <TimeAgo iso={action.approval.decided_at} />
          {action.approval.comment && ` — “${action.approval.comment}”`}
          {action.approval.consumed_at && " · consumed by one execution"}
        </p>
      )}
      <ExecutionStatus action={action} onChanged={onChanged} />
      {decide && pending && (
        <div className="pp-decide">
          <label className="pp-field">
            Comment (optional)
            <input value={comment} maxLength={500} onChange={(e) => setComment(e.target.value)} placeholder="Reason for your decision" />
          </label>
          <div className="pp-decide-buttons">
            <button className="pp-btn ghost" disabled={!!busy || !action.can_decide} onClick={() => act("reject")}>
              <X size={14} /> {busy === "reject" ? "Rejecting…" : "Reject"}
            </button>
            <button className="pp-btn primary" disabled={!!busy || !action.can_decide} onClick={() => act("approve")}>
              <Check size={14} /> {busy === "approve" ? "Approving…" : "Approve exact action"}
            </button>
          </div>
          {!action.can_decide && (
            <p className="pp-note small">
              Your role cannot decide this action. It requires <strong>{roleLabel(action.required_role ?? "sales_manager")}</strong> or higher
              {action.policy_outcome.annotations?.separation_of_duties ? ", and someone other than the person who started the run" : ""}.
            </p>
          )}
        </div>
      )}
      {error && <p className="pp-note error" role="alert">{error}</p>}
    </article>
  );
}
