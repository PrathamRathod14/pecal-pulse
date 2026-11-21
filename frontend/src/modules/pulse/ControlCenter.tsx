"use client";
import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Play, Search } from "lucide-react";
import type { Scenario, Signal } from "@/types/pulse";
import { pulseApi } from "./api";
import { usePoll, usePulseSession } from "./hooks";
import { RunDetailView } from "./RunDetail";
import { Async, Kpi, Panel, Pill, SCENARIO_LABELS, StatusPill, TimeAgo } from "./ui";

export function ControlCenter() {
  const session = usePulseSession();
  const router = useRouter();
  const params = useSearchParams();
  const selected = params.get("run");
  const overview = usePoll(() => pulseApi.overview(), 5000, [session.actor?.id]);
  const runs = usePoll(() => pulseApi.runs(25), 3000, [session.actor?.id]);
  const select = (id: string) => router.replace(`/pulse?run=${id}`, { scroll: false });

  useEffect(() => {
    if (!selected && runs.data?.items[0]) select(runs.data.items.find((r) => r.scenario !== "security_probe")?.id ?? runs.data.items[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runs.data, selected]);

  return (
    <div className="pp-page">
      <Async state={overview}>
        {(o) => (
          <div className="pp-kpis">
            <Kpi label="Critical signals" value={o.signals.critical} hint={`${o.signals.high} high · ${o.signals.opportunity} growth`} tone="red" />
            <Kpi
              label="Calibrations / yr at risk"
              value={o.signals.volume_at_risk_12m.toLocaleString()}
              hint="Gap between prior and recent pace (critical + high)"
            />
            <Kpi label="Pending approvals" value={o.pending_approvals} hint="Waiting for a manager" tone={o.pending_approvals ? "orange" : undefined} />
            <Kpi
              label="Agent runs"
              value={Object.values(o.runs).reduce((a, b) => a + b, 0)}
              hint={`${o.runs.completed ?? 0} completed · ${(o.runs.running ?? 0) + (o.runs.awaiting_approval ?? 0)} active`}
            />
            <Kpi
              label="PulseGuard decisions"
              value={Object.values(o.policy_outcomes).reduce((a, b) => a + b, 0)}
              hint={`${o.policy_outcomes.deny ?? 0} denied · ${o.policy_outcomes.require_approval ?? 0} need approval`}
              tone="green"
            />
          </div>
        )}
      </Async>
      <div className="pp-grid-cc">
        <div className="pp-col">
          <SignalsPanel onStarted={(id) => { runs.reload(); overview.reload(); select(id); }} />
          <Panel title="Agent runs" subtitle="Most recent first. Probe runs are security tests, not investigations.">
            <Async state={runs} isEmpty={(d) => !d.items.length} empty="No investigations yet — start one from a signal above.">
              {(d) => (
                <ul className="pp-run-list">
                  {d.items.map((r) => (
                    <li key={r.id}>
                      <button className={r.id === selected ? "active" : ""} onClick={() => select(r.id)} aria-current={r.id === selected}>
                        <span className="pp-run-main">
                          <strong>{r.customer_id}</strong>
                          <StatusPill value={r.status} />
                          {r.scenario && r.scenario !== "standard" && (
                            <Pill tone="purple">{r.scenario === "security_probe" ? "security probe" : SCENARIO_LABELS[r.scenario] ?? r.scenario}</Pill>
                          )}
                        </span>
                        <span className="pp-run-meta">
                          {r.id} · <TimeAgo iso={r.created_at} />
                          {r.llm_source && ` · ${r.llm_source === "gemini" ? "Gemini" : "fallback"}`}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </Async>
          </Panel>
        </div>
        <div className="pp-col wide">
          {selected ? <RunDetailView runId={selected} /> : <Panel title="Run detail"><p className="pp-muted">Select a run.</p></Panel>}
        </div>
      </div>
    </div>
  );
}

function SignalsPanel({ onStarted }: { onStarted: (runId: string) => void }) {
  const session = usePulseSession();
  const signals = usePoll(() => pulseApi.signals(40), 15000);
  const [scenario, setScenario] = useState<Scenario>("standard");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const canStart = session.can("investigation:start");
  const scenarios = session.status?.scenarios ?? ["standard"];
  const start = async (s: Signal) => {
    setBusy(s.customer_id);
    setError(null);
    try {
      const r = await pulseApi.startRun(s.customer_id, scenario);
      onStarted(r.run_id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };
  return (
    <Panel
      title="Business signals"
      subtitle={signals.data?.basis ?? "Measured from monthly calibration history."}
      actions={
        scenarios.length > 1 && (
          <label className="pp-inline-field">
            Scenario
            <select value={scenario} onChange={(e) => setScenario(e.target.value as Scenario)}>
              {scenarios.map((s) => (
                <option key={s} value={s}>
                  {SCENARIO_LABELS[s] ?? s}
                </option>
              ))}
            </select>
          </label>
        )
      }
    >
      {!canStart && <p className="pp-note">Your role can view signals but not start investigations (needs analyst or higher).</p>}
      {error && <p className="pp-note error" role="alert">{error}</p>}
      <label className="pp-search">
        <Search size={14} aria-hidden />
        <input placeholder="Filter by customer or industry" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Filter signals" />
      </label>
      <Async state={signals} isEmpty={(d) => !d.items.length} empty="No risk or growth signals in the active snapshot.">
        {(d) => (
          <ul className="pp-signal-list">
            {d.items
              .filter((s) => !query || `${s.customer_id} ${s.display_name} ${s.industry}`.toLowerCase().includes(query.toLowerCase()))
              .map((s) => (
                <li key={`${s.customer_id}-${s.key}`} className={`sev-${s.severity}`}>
                  <div>
                    <div className="pp-signal-head">
                      <StatusPill value={s.severity} />
                      <strong>{s.display_name}</strong>
                      <span className="pp-muted">{s.industry ?? "—"} · {s.value_tier}</span>
                    </div>
                    <p className="pp-signal-title">{s.title}</p>
                    <p className="pp-muted small">{s.fact}</p>
                    {s.latest_run && (
                      <p className="pp-muted small">
                        Last run <StatusPill value={s.latest_run.status} /> <TimeAgo iso={s.latest_run.created_at} />
                      </p>
                    )}
                  </div>
                  <button
                    className="pp-btn primary small"
                    disabled={!canStart || !!busy}
                    title={canStart ? `Start a ${SCENARIO_LABELS[scenario]} for ${s.customer_id}` : "Requires analyst role or higher"}
                    onClick={() => start(s)}
                  >
                    <Play size={13} /> {busy === s.customer_id ? "Starting…" : "Investigate"}
                  </button>
                </li>
              ))}
          </ul>
        )}
      </Async>
    </Panel>
  );
}
