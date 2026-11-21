"use client";
import { useState } from "react";
import { FlaskConical } from "lucide-react";
import type { ProbeResult } from "@/types/pulse";
import { pulseApi } from "./api";
import { usePoll, usePulseSession } from "./hooks";
import { roleLabel } from "./PulseLayout";
import { Async, Disclosure, Empty, JsonView, Kpi, Panel, Pill, StatusPill, TimeAgo } from "./ui";

const PROBES = [
  { key: "unknown_action", label: "Unknown action", desc: "Agent proposes export_customer_data with approved=true" },
  { key: "unauthorized", label: "Unauthorized approver", desc: "Analyst tries to approve a customer e-mail" },
  { key: "replay", label: "Approval replay", desc: "Reuse a consumed approval for a second execution" },
  { key: "tamper", label: "Payload tampering", desc: "Rewrite the e-mail and its hash after approval" },
  { key: "forged_webhook", label: "Forged webhook", desc: "Callback with an invalid HMAC signature" },
  { key: "duplicate_callback", label: "Duplicate callback", desc: "Deliver the same n8n result twice, then a conflicting one" },
];

export function Security() {
  const session = usePulseSession();
  const canRead = session.can("security:read");
  const policy = usePoll(() => pulseApi.policy(), 0);
  const decisions = usePoll(() => (canRead ? pulseApi.decisions() : Promise.resolve({ items: [] })), 5000, [canRead]);
  const events = usePoll(() => (canRead ? pulseApi.securityEvents() : Promise.resolve({ items: [] })), 5000, [canRead]);
  const counts = (decisions.data?.items ?? []).reduce<Record<string, number>>((acc, d) => ((acc[d.outcome] = (acc[d.outcome] ?? 0) + 1), acc), {});
  return (
    <div className="pp-page">
      <div className="pp-kpis">
        <Kpi label="Allowed" value={counts.allow ?? 0} tone="green" hint="Low-risk, policy auto-approved" />
        <Kpi label="Approval required" value={counts.require_approval ?? 0} tone="orange" />
        <Kpi label="Denied" value={counts.deny ?? 0} tone="red" hint="Blocked before any human saw them" />
        <Kpi label="Security events" value={events.data?.items.length ?? 0} hint="Refusals, forged inputs, replays" />
      </div>
      <Panel title="Policy" subtitle="Deterministic and server-side. No model output can change these rules; anything not listed is denied.">
        <Async state={policy}>
          {(p) => (
            <>
              <div className="pp-table-wrap">
                <table className="pp-table">
                  <thead><tr><th>Action</th><th>Risk</th><th>Approval</th><th>External</th><th>Destination</th></tr></thead>
                  <tbody>
                    {p.actions.map((a) => (
                      <tr key={a.action_type}>
                        <td><strong>{a.label}</strong><br /><code className="small">{a.action_type}</code></td>
                        <td><StatusPill value={a.risk} /></td>
                        <td>{a.approval}</td>
                        <td>{a.external ? "yes" : "no"}</td>
                        <td><code>{a.destination}</code></td>
                      </tr>
                    ))}
                    <tr><td colSpan={5}><Pill tone="red">default</Pill> {p.default}</td></tr>
                  </tbody>
                </table>
              </div>
              <div className="pp-two">
                <div>
                  <h4>Limits</h4>
                  <ul className="pp-facts">
                    <li>Discounts ≤ {p.limits.max_discount_pct}% for ≤ {p.limits.max_discount_months} months, director approval, separation of duties</li>
                    <li>Approvals expire after {Math.round(p.limits.approval_ttl_seconds / 60)} min and are single-use</li>
                    <li>Only <code>{p.proposers.join(", ")}</code> may propose actions</li>
                    <li>Customer e-mails go only to the verified contact on file; no links or foreign addresses</li>
                    <li>Instruction-like text (prompt injection) in any payload field blocks the action</li>
                  </ul>
                </div>
                <div>
                  <h4>Roles</h4>
                  <ul className="pp-facts">
                    {Object.entries(p.roles).map(([role, perms]) => (
                      <li key={role}><strong>{roleLabel(role)}</strong>: {perms.join(", ")}</li>
                    ))}
                  </ul>
                </div>
              </div>
            </>
          )}
        </Async>
      </Panel>
      <ProbesPanel onDone={() => { decisions.reload(); events.reload(); }} />
      {!canRead ? (
        <Panel title="Decisions and security events"><Empty>Your role cannot read security records (requires sales manager or director).</Empty></Panel>
      ) : (
        <div className="pp-grid-2">
          <Panel title="Policy decisions" subtitle="Proposal, approval and execution-time evaluations.">
            <Async state={decisions} isEmpty={(d) => !d.items.length} empty="No decisions yet.">
              {(d) => (
                <ul className="pp-feed">
                  {d.items.map((x) => (
                    <li key={x.id}>
                      <div className="pp-feed-head">
                        <StatusPill value={x.outcome} />
                        <code>{x.action_type}</code>
                        <Pill tone="neutral">{x.phase}</Pill>
                        <span className="pp-muted small"><TimeAgo iso={x.created_at} /></span>
                      </div>
                      <p className="small">{x.reasons.join(" ")}</p>
                      <p className="pp-muted small">{x.rules.join(" · ")}</p>
                    </li>
                  ))}
                </ul>
              )}
            </Async>
          </Panel>
          <Panel title="Security events" subtitle="Every refusal is recorded, even when the request itself is rolled back.">
            <Async state={events} isEmpty={(d) => !d.items.length} empty="No security events yet.">
              {(d) => (
                <ul className="pp-feed">
                  {d.items.map((x) => (
                    <li key={x.id}>
                      <div className="pp-feed-head">
                        <Pill tone={x.severity === "critical" || x.severity === "high" ? "red" : "orange"}>{x.severity}</Pill>
                        <strong>{x.kind.replace(/_/g, " ")}</strong>
                        <span className="pp-muted small">{x.actor} · <TimeAgo iso={x.created_at} /></span>
                      </div>
                      <p className="small">{String((x.detail as { reason?: string }).reason ?? (x.detail as { reasons?: string[] }).reasons?.join(" ") ?? "")}</p>
                      <Disclosure summary="Details"><JsonView value={x.detail} /></Disclosure>
                    </li>
                  ))}
                </ul>
              )}
            </Async>
          </Panel>
        </div>
      )}
    </div>
  );
}

function ProbesPanel({ onDone }: { onDone: () => void }) {
  const session = usePulseSession();
  const allowed = session.can("security:probe");
  const [results, setResults] = useState<Record<string, ProbeResult | { error: string }>>({});
  const [busy, setBusy] = useState<string | null>(null);
  return (
    <Panel
      title="Live security probes"
      subtitle="Each probe performs a real attack attempt through the same enforcement code the agents and API use, then reports what PulseGuard did. Nothing is sent to n8n."
    >
      {!allowed && <p className="pp-note">Probes require the Director role (switch identity in the header).</p>}
      <div className="pp-probe-grid">
        {PROBES.map((p) => {
          const r = results[p.key];
          return (
            <article key={p.key} className="pp-probe">
              <h3>{p.label}</h3>
              <p className="pp-muted small">{p.desc}</p>
              <button
                className="pp-btn ghost small"
                disabled={!allowed || !!busy}
                onClick={async () => {
                  setBusy(p.key);
                  try {
                    const res = await pulseApi.probe(p.key);
                    setResults((x) => ({ ...x, [p.key]: res }));
                  } catch (e) {
                    setResults((x) => ({ ...x, [p.key]: { error: (e as Error).message } }));
                  } finally {
                    setBusy(null);
                    onDone();
                  }
                }}
              >
                <FlaskConical size={13} /> {busy === p.key ? "Running…" : "Run probe"}
              </button>
              {r && "error" in r && <p className="pp-note error small">{r.error}</p>}
              {r && "probe" in r && (
                <div className="pp-probe-result">
                  <Pill tone={r.blocked ? "green" : r.blocked === false ? "red" : "neutral"}>
                    {r.blocked ? "blocked ✓" : r.blocked === false ? "NOT blocked" : "not applicable"}
                  </Pill>
                  <p className="small">Expected: {r.expected}</p>
                  <Disclosure summary="Observed"><JsonView value={r.observed} /></Disclosure>
                </div>
              )}
            </article>
          );
        })}
      </div>
    </Panel>
  );
}
