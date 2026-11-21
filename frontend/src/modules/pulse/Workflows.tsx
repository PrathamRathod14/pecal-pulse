"use client";
import { useState } from "react";
import { Radar, RotateCw } from "lucide-react";
import { pulseApi } from "./api";
import { usePoll, usePulseSession } from "./hooks";
import { Async, Disclosure, JsonView, Kpi, Panel, Pill, StatusPill, TimeAgo } from "./ui";

export function Workflows() {
  const session = usePulseSession();
  const info = usePoll(() => pulseApi.workflows(), 6000, [session.actor?.id]);
  const executions = usePoll(() => pulseApi.executions(), 4000, [session.actor?.id]);
  const [scan, setScan] = useState<{ busy: boolean; result?: string; error?: string }>({ busy: false });
  const [retrying, setRetrying] = useState<string | null>(null);
  const [retryError, setRetryError] = useState<string | null>(null);
  const canScan = session.can("action:approve:standard");
  const canRetry = session.can("execution:retry");
  return (
    <div className="pp-page">
      <Async state={info}>
        {(w) => (
          <>
            <div className="pp-kpis">
              <Kpi label="n8n" value={w.n8n.reachable ? "Online" : "Offline"} hint={w.n8n.url} tone={w.n8n.reachable ? "green" : "red"} />
              <Kpi label="Succeeded" value={w.execution_stats.succeeded ?? 0} tone="green" />
              <Kpi label="In flight" value={(w.execution_stats.submitted ?? 0) + (w.execution_stats.dispatching ?? 0) + (w.execution_stats.authorized ?? 0)} />
              <Kpi label="Failed" value={(w.execution_stats.failed ?? 0) + (w.execution_stats.dispatch_failed ?? 0)} tone={(w.execution_stats.failed ?? 0) + (w.execution_stats.dispatch_failed ?? 0) ? "red" : undefined} />
            </div>
            {!w.n8n.reachable && (
              <p className="pp-note error" role="alert">
                n8n is not reachable at {w.n8n.url}{w.n8n.error ? ` (${w.n8n.error})` : ""}. Approved actions will be recorded as
                <strong> dispatch failed</strong> and can be retried once n8n is back. Start it with <code>docker compose up -d n8n</code>.
              </p>
            )}
            <Panel
              title="n8n workflows"
              subtitle={<>Backend → n8n: <code>{w.execute_webhook}</code> (HMAC-signed). n8n → backend: <code>{w.callback_url}</code> (HMAC-signed, deduplicated).</>}
              actions={
                <button
                  className="pp-btn ghost small"
                  disabled={!canScan || scan.busy || !w.n8n.reachable}
                  title={!canScan ? "Requires sales manager or director" : !w.n8n.reachable ? "n8n is offline" : "Runs the risk-scan workflow in n8n, which asks Pulse to investigate new critical signals"}
                  onClick={async () => {
                    setScan({ busy: true });
                    try {
                      const r = await pulseApi.triggerScan();
                      const res = r.result as { started?: { customer_id: string }[]; already_triggered_today?: string[] } | null;
                      setScan({
                        busy: false,
                        result: res?.started
                          ? `n8n started ${res.started.length} investigation(s)${res.started.length ? `: ${res.started.map((s) => s.customer_id).join(", ")}` : ""}; ${res.already_triggered_today?.length ?? 0} already handled today.`
                          : `n8n responded HTTP ${r.n8n_status}.`,
                      });
                    } catch (e) {
                      setScan({ busy: false, error: (e as Error).message });
                    }
                  }}
                >
                  <Radar size={13} /> {scan.busy ? "Scanning via n8n…" : "Run risk scan via n8n"}
                </button>
              }
            >
              {scan.result && <p className="pp-note">{scan.result}</p>}
              {scan.error && <p className="pp-note error" role="alert">{scan.error}</p>}
              <div className="pp-wf-grid">
                {w.workflows.map((wf) => (
                  <article key={wf.id} className="pp-wf">
                    <h3>{wf.name}</h3>
                    <p className="pp-muted small"><code>{wf.id}</code> · {wf.trigger}</p>
                    <p>{wf.purpose}</p>
                    {wf.handlers && (
                      <ul className="pp-facts">
                        {Object.entries(wf.handlers).map(([k, v]) => (
                          <li key={k}><code>{k}</code>: {v}</li>
                        ))}
                      </ul>
                    )}
                  </article>
                ))}
              </div>
            </Panel>
          </>
        )}
      </Async>
      <Panel title="Execution history" subtitle="Every execution was authorized by PulseGuard immediately before dispatch. Results are written only from signed n8n callbacks.">
        {retryError && <p className="pp-note error" role="alert">{retryError}</p>}
        <Async state={executions} isEmpty={(d) => !d.items.length} empty="No executions yet. Approved actions appear here.">
          {(d) => (
            <div className="pp-table-wrap">
              <table className="pp-table">
                <thead>
                  <tr><th>Execution</th><th>Action</th><th>Customer</th><th>Status</th><th>Delivery</th><th>n8n #</th><th>Attempts</th><th>Updated</th><th /></tr>
                </thead>
                <tbody>
                  {d.items.map((e) => {
                    const result = e.result as { delivery?: string } | null;
                    return (
                      <tr key={e.id}>
                        <td>
                          <Disclosure summary={<code>{e.id}</code>}>
                            <p className="pp-muted small">Idempotency key <code>{e.idempotency_key}</code> · destination {e.destination}</p>
                            {e.error && <p className="pp-note error small">{e.error}</p>}
                            {e.result && <JsonView value={e.result} />}
                          </Disclosure>
                        </td>
                        <td>{e.action_type?.replace(/_/g, " ")}</td>
                        <td>{e.customer_id}</td>
                        <td><StatusPill value={e.status} /></td>
                        <td>{result?.delivery ? <Pill tone={result.delivery === "simulated" ? "orange" : "green"}>{result.delivery}</Pill> : "—"}</td>
                        <td>{e.n8n_execution_id ?? "—"}</td>
                        <td>{e.attempts}</td>
                        <td><TimeAgo iso={e.updated_at} /></td>
                        <td>
                          {e.status === "dispatch_failed" && (
                            <button
                              className="pp-btn ghost small"
                              disabled={!canRetry || retrying === e.id}
                              title={canRetry ? "Re-authorize and resend with the same idempotency key" : "Requires sales manager or director"}
                              onClick={async () => {
                                setRetrying(e.id);
                                setRetryError(null);
                                try {
                                  await pulseApi.retry(e.id);
                                } catch (err) {
                                  setRetryError((err as Error).message);
                                } finally {
                                  setRetrying(null);
                                  executions.reload();
                                }
                              }}
                            >
                              <RotateCw size={12} /> Retry
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Async>
      </Panel>
    </div>
  );
}
