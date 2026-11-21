"use client";
import Link from "next/link";
import { useState } from "react";
import type { PulseAction } from "@/types/pulse";
import { ActionCard } from "./ActionCard";
import { pulseApi } from "./api";
import { usePoll, usePulseSession } from "./hooks";
import { Async, Disclosure, ErrorState, Loading, Panel, Sparkline, StatusPill } from "./ui";

const FILTERS = [
  { key: "pending_approval", label: "Pending" },
  { key: "decided", label: "Decided" },
  { key: "blocked", label: "Blocked by PulseGuard" },
  { key: "all", label: "All" },
] as const;

const DECIDED = new Set(["approved", "rejected", "executing", "executed", "execution_failed", "expired", "auto_approved"]);

export function Approvals() {
  const session = usePulseSession();
  const [filter, setFilter] = useState<(typeof FILTERS)[number]["key"]>("pending_approval");
  const actions = usePoll(
    () => pulseApi.actions(filter === "pending_approval" || filter === "blocked" ? filter : undefined),
    4000,
    [filter, session.actor?.id],
  );
  const visible = (items: PulseAction[]) =>
    filter === "decided" ? items.filter((a) => DECIDED.has(a.status)) : items;
  return (
    <div className="pp-page">
      <Panel
        title="Approval Center"
        subtitle="Each approval authorizes one exact payload (hash-bound), expires, and can be used by exactly one execution. Authorization is enforced by the backend, not this page."
        actions={
          <div className="pp-segmented" role="tablist" aria-label="Filter actions">
            {FILTERS.map((f) => (
              <button key={f.key} role="tab" aria-selected={filter === f.key} className={filter === f.key ? "active" : ""} onClick={() => setFilter(f.key)}>
                {f.label}
              </button>
            ))}
          </div>
        }
      >
        {!session.can("action:approve:standard") && (
          <p className="pp-note">
            You are signed in as <strong>{session.actor?.name}</strong>. Your role can review but not approve; switch identity in the header to act as a manager.
          </p>
        )}
        <Async
          state={actions}
          isEmpty={(d) => !visible(d.items).length}
          empty={filter === "pending_approval" ? "Nothing is waiting for a decision. Start an investigation in the Agent Control Center." : "No actions in this view."}
        >
          {(d) => (
            <div className="pp-approval-list">
              {visible(d.items).map((a) => (
                <div key={a.id} className="pp-approval">
                  <ActionCard action={a} decide onChanged={actions.reload} />
                  <aside>
                    {a.run && (
                      <div className="pp-side-block">
                        <h4>Agent recommendation</h4>
                        <p>{a.run.summary ?? "—"}</p>
                        <p className="pp-muted small">
                          Run <Link href={`/pulse?run=${a.run.id}`}><code>{a.run.id}</code></Link> ·{" "}
                          {a.run.llm_source === "gemini" ? "Gemini" : "deterministic fallback"} · started by {a.run.started_by}
                        </p>
                      </div>
                    )}
                    <CustomerContextBlock customerId={a.customer_id} />
                  </aside>
                </div>
              ))}
            </div>
          )}
        </Async>
      </Panel>
    </div>
  );
}

function CustomerContextBlock({ customerId }: { customerId: string }) {
  const ctx = usePoll(() => pulseApi.customerContext(customerId), 0, [customerId]);
  if (ctx.error) return <ErrorState error={ctx.error} onRetry={ctx.reload} />;
  if (!ctx.data) return <Loading label="Loading customer context…" />;
  const c = ctx.data;
  return (
    <div className="pp-side-block">
      <h4>Customer context</h4>
      <p>
        <strong>{c.profile.display_name}</strong>
        <br />
        <span className="pp-muted small">{c.profile.industry ?? "—"} · owner {c.crm.account_owner}</span>
      </p>
      <Sparkline series={c.activity.series_last_15m} />
      <ul className="pp-facts">
        <li>12-month volume: {c.activity.volume_12m}</li>
        <li>
          Recent pace {c.activity.avg_last_3m}/mo vs {c.activity.avg_prior_12m}/mo
          {c.activity.change_pct != null && ` (${c.activity.change_pct > 0 ? "+" : ""}${c.activity.change_pct}%)`}
        </li>
        <li>Due: {c.due_requirements.overdue} overdue · {c.due_requirements.upcoming} upcoming</li>
        <li>Peer median ({c.peers.industry ?? "industry"}): {c.peers.peer_median_12m ?? "—"}</li>
      </ul>
      {c.activity.signals.map((s) => (
        <p key={s.key} className="small"><StatusPill value={s.severity} /> {s.title}</p>
      ))}
      {c.crm.notes.length > 0 && (
        <Disclosure summary={`${c.crm.notes.length} CRM note(s) (untrusted text)`}>
          <ul className="pp-notes">{c.crm.notes.map((n) => <li key={n.date + n.text}><span className="pp-muted small">{n.date}</span> <q>{n.text}</q></li>)}</ul>
        </Disclosure>
      )}
      {c.crm.synthetic && <p className="pp-muted small">Synthetic demo CRM data.</p>}
    </div>
  );
}
