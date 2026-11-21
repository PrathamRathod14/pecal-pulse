"use client";
import Link from "next/link";
import { Fragment, useEffect, useState } from "react";
import { AlertTriangle, ArrowRight, Wrench } from "lucide-react";
import type { AgentStep, RunDetail } from "@/types/pulse";
import { pulseApi } from "./api";
import { ActionCard } from "./ActionCard";
import { usePoll, usePulseSession } from "./hooks";
import { AGENT_LABELS, Async, Disclosure, JsonView, Panel, Pill, SCENARIO_LABELS, Sparkline, StatusPill, TimeAgo } from "./ui";

const PIPELINE: (string | string[])[] = [
  "orchestrator",
  "customer_intelligence",
  ["risk_detection", "opportunity_intelligence"],
  "strategy",
  "pulseguard",
  "approval_gate",
  "workflow_execution",
  "audit",
];

const ACTIVE = new Set(["running", "awaiting_approval", "awaiting_callback"]);

function lastStep(steps: AgentStep[], agent: string) {
  return [...steps].reverse().find((s) => s.agent === agent);
}

function Pipeline({ detail }: { detail: RunDetail }) {
  const node = (agent: string) => {
    const s = lastStep(detail.steps, agent);
    const retries = detail.steps.filter((x) => x.agent === agent && x.status === "retrying").length;
    const state = s?.status ?? (detail.run.status === "failed" ? "skipped" : "pending");
    return (
      <div key={agent} className={`pp-stage ${state}`} title={s?.error ?? undefined}>
        <span className="pp-stage-dot" aria-hidden />
        <span className="pp-stage-name">{AGENT_LABELS[agent]}</span>
        <span className="pp-stage-meta">
          {state}
          {s?.duration_ms != null && ` · ${s.duration_ms} ms`}
          {retries > 0 && ` · ${retries} retry`}
        </span>
      </div>
    );
  };
  return (
    <div className="pp-pipeline" role="list" aria-label="Agent pipeline">
      {PIPELINE.map((p, i) => (
        <Fragment key={i}>
          {i > 0 && <ArrowRight size={14} className="pp-arrow" aria-hidden />}
          {Array.isArray(p) ? <div className="pp-parallel" aria-label="Runs in parallel">{p.map(node)}</div> : node(p)}
        </Fragment>
      ))}
    </div>
  );
}

type StrategyOut = {
  output: {
    summary: string;
    risk_assessment: string;
    confidence: number;
    confidence_rationale: string;
    expected_impact: string;
    recommendations: string[];
    measured_facts_used: string[];
    assumptions: string[];
  };
  llm: { source: string; model?: string; reason?: string; category?: string; attempts?: number; input_tokens?: number; output_tokens?: number };
};

function Recommendation({ detail }: { detail: RunDetail }) {
  const step = lastStep(detail.steps, "strategy");
  const out = (step?.output as { strategy?: StrategyOut } | null)?.strategy;
  if (!out) return null;
  const o = out.output;
  const gemini = out.llm.source === "gemini";
  return (
    <section className="pp-reco">
      <header>
        <h3>Recommendation</h3>
        <Pill tone={gemini ? "green" : "orange"} title={out.llm.reason}>
          {gemini ? `Gemini · ${out.llm.model}` : "Deterministic fallback (not Gemini)"}
        </Pill>
      </header>
      {!gemini && out.llm.reason && <p className="pp-note small">Why fallback: {out.llm.reason}</p>}
      <p className="pp-reco-summary">{o.summary}</p>
      <div className="pp-confidence" aria-label={`Confidence ${Math.round(o.confidence * 100)}%`}>
        <span>Confidence</span>
        <div className="pp-bar"><i style={{ width: `${Math.round(o.confidence * 100)}%` }} /></div>
        <strong>{Math.round(o.confidence * 100)}%</strong>
      </div>
      <p className="pp-muted small">{o.confidence_rationale}</p>
      <div className="pp-two">
        <div>
          <h4>Measured facts</h4>
          <ul>{o.measured_facts_used.map((f) => <li key={f}>{f}</li>)}</ul>
        </div>
        <div>
          <h4>Assumptions (not measured)</h4>
          {o.assumptions.length ? <ul>{o.assumptions.map((f) => <li key={f}>{f}</li>)}</ul> : <p className="pp-muted small">None stated.</p>}
        </div>
      </div>
      <h4>Expected impact</h4>
      <p>{o.expected_impact}</p>
      <h4>Next steps</h4>
      <ol>{o.recommendations.map((r) => <li key={r}>{r}</li>)}</ol>
    </section>
  );
}

function Evidence({ detail }: { detail: RunDetail }) {
  const ci = lastStep(detail.steps, "customer_intelligence")?.output as { customer?: Record<string, any> } | null;
  const risk = lastStep(detail.steps, "risk_detection")?.output as { risk?: Record<string, any> } | null;
  const opp = lastStep(detail.steps, "opportunity_intelligence")?.output as { opportunity?: Record<string, any> } | null;
  const c = ci?.customer;
  if (!c) return null;
  return (
    <section className="pp-evidence">
      <div className="pp-evidence-head">
        <div>
          <h3>{c.profile.display_name}</h3>
          <p className="pp-muted small">
            {c.profile.industry ?? "Industry unknown"} · 12-month volume {c.activity.volume_12m} calibrations
            {c.value_tier && ` · ${c.value_tier}`}
          </p>
        </div>
        <div className="pp-spark-wrap">
          <Sparkline series={c.activity.series_last_15m} />
          <span className="pp-muted small">last 15 months (latest 3 highlighted)</span>
        </div>
      </div>
      <div className="pp-three">
        <div>
          <h4>Customer intelligence</h4>
          <ul>
            {(c.behavior_changes as string[]).map((b) => <li key={b}>{b}</li>)}
            <li>Due: {c.due_requirements.overdue} overdue · {c.due_requirements.upcoming} upcoming</li>
            <li>Peers: median {c.peers.peer_median_12m ?? "—"} / 12 mo ({c.peers.peer_count} in {c.peers.industry ?? "industry"})</li>
            <li>Contact on file: {c.crm.has_verified_contact ? "verified" : "none"} · owner {c.crm.account_owner}</li>
          </ul>
        </div>
        <div>
          <h4>Risk {risk?.risk && <StatusPill value={risk.risk.level} />}</h4>
          <ul>{(risk?.risk?.drivers ?? []).map((d: string) => <li key={d}>{d}</li>)}</ul>
          {(risk?.risk?.predictions ?? []).map((p: string) => <p key={p} className="pp-muted small">Projection: {p}</p>)}
        </div>
        <div>
          <h4>Opportunity {opp?.opportunity && <StatusPill value={opp.opportunity.level} />}</h4>
          <ul>{(opp?.opportunity?.measured_facts ?? []).map((d: string) => <li key={d}>{d}</li>)}</ul>
          {(opp?.opportunity?.ideas ?? []).map((d: string) => <p key={d} className="pp-muted small">{d}</p>)}
        </div>
      </div>
      {c.crm_notes?.length > 0 && (
        <Disclosure summary={`${c.crm_notes.length} CRM note(s) — untrusted text, passed to Strategy as data only`}>
          <ul className="pp-notes">
            {c.crm_notes.map((n: { date: string; author: string; text: string }) => (
              <li key={n.date + n.text}><span className="pp-muted small">{n.date} · {n.author}</span><q>{n.text}</q></li>
            ))}
          </ul>
        </Disclosure>
      )}
    </section>
  );
}

function Trace({ steps }: { steps: AgentStep[] }) {
  return (
    <ol className="pp-trace">
      {steps.map((s) => (
        <li key={s.id} className={s.status}>
          <Disclosure
            summary={
              <span className="pp-trace-head">
                <strong>{AGENT_LABELS[s.agent] ?? s.agent}</strong>
                <StatusPill value={s.status} />
                {s.attempt > 1 && <Pill tone="orange">attempt {s.attempt}</Pill>}
                <span className="pp-muted small">
                  {s.duration_ms != null ? `${s.duration_ms} ms` : ""} {s.tool_calls?.length ? `· ${s.tool_calls.length} tool call(s)` : ""}
                </span>
              </span>
            }
          >
            {s.error && <p className="pp-note error small"><AlertTriangle size={12} /> {s.error}</p>}
            {!!s.tool_calls?.length && (
              <table className="pp-table compact">
                <thead><tr><th>Tool</th><th>Result</th><th>ms</th></tr></thead>
                <tbody>
                  {s.tool_calls.map((t, i) => (
                    <tr key={i}>
                      <td><Wrench size={11} /> <code>{t.tool}</code></td>
                      <td>{t.ok ? t.result_summary : <span className="pp-error-text">{t.error}</span>}</td>
                      <td>{t.ms ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <JsonView value={s.output} />
          </Disclosure>
        </li>
      ))}
    </ol>
  );
}

export function RunDetailView({ runId }: { runId: string }) {
  const session = usePulseSession();
  const [interval, setIntervalMs] = useState(1500);
  const detail = usePoll(() => pulseApi.run(runId), interval, [runId, session.actor?.id]);
  const status = detail.data?.run.status;
  useEffect(() => {
    if (status) setIntervalMs(ACTIVE.has(status) ? 1500 : 10000);
  }, [status]);
  return (
    <Async state={detail}>
      {(d) => (
        <Panel
          title={<>Run <code>{d.run.id}</code></>}
          subtitle={
            <>
              {d.run.objective} · started by {d.run.started_by} <TimeAgo iso={d.run.created_at} />
              {d.run.scenario && d.run.scenario !== "standard" && ` · scenario: ${SCENARIO_LABELS[d.run.scenario] ?? d.run.scenario}`}
            </>
          }
          actions={<StatusPill value={d.run.status} />}
        >
          {d.run.error && <p className="pp-note error" role="alert">{d.run.error}</p>}
          {d.run.scenario !== "security_probe" && <Pipeline detail={d} />}
          {d.run.status === "awaiting_approval" && (
            <p className="pp-note">
              Paused at the approval gate (LangGraph interrupt, checkpointed). It resumes automatically after the last decision —{" "}
              <Link href="/pulse/approvals">open Approvals</Link>.
            </p>
          )}
          <Evidence detail={d} />
          <Recommendation detail={d} />
          <h3 className="pp-section-title">Proposed actions ({d.actions.length})</h3>
          {d.actions.length ? (
            <div className="pp-actions">{d.actions.map((a) => <ActionCard key={a.id} action={a} onChanged={detail.reload} />)}</div>
          ) : (
            <p className="pp-muted">No actions proposed yet.</p>
          )}
          <h3 className="pp-section-title">Execution trace</h3>
          <Trace steps={d.steps} />
        </Panel>
      )}
    </Async>
  );
}
