"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { ShieldCheck, ShieldX } from "lucide-react";
import type { AuditEvent } from "@/types/pulse";
import { pulseApi } from "./api";
import { usePoll, usePulseSession } from "./hooks";
import { Async, Disclosure, Empty, Hash, JsonView, Panel, Pill } from "./ui";

const KINDS = ["", "run.", "action.", "approval.", "execution.", "callback.", "security.", "signal.", "auth.", "workflow."];

function tone(kind: string) {
  if (kind.includes("blocked") || kind.includes("failed") || kind.includes("rejected")) return "red" as const;
  if (kind.startsWith("approval.granted") || kind.includes("succeeded") || kind.includes("completed") || kind.includes("auto_approved")) return "green" as const;
  if (kind.includes("pending") || kind.includes("expired")) return "orange" as const;
  if (kind.startsWith("security")) return "purple" as const;
  return "neutral" as const;
}

export function AuditTrail() {
  const session = usePulseSession();
  const [kind, setKind] = useState("");
  const [runId, setRunId] = useState("");
  const [older, setOlder] = useState<AuditEvent[]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const canRead = session.can("audit:read");
  const trail = usePoll(() => (canRead ? pulseApi.audit({ run_id: runId || undefined, limit: 100 }) : Promise.reject(new Error("Your role cannot read the audit trail (requires analyst or higher)."))), 5000, [runId, canRead]);
  useEffect(() => setOlder([]), [runId]);
  const filter = (items: AuditEvent[]) => (kind ? items.filter((e) => e.kind.startsWith(kind)) : items);
  if (!canRead) return <div className="pp-page"><Panel title="Audit trail"><Empty>Your role cannot read the audit trail (requires analyst or higher).</Empty></Panel></div>;
  return (
    <div className="pp-page">
      <Panel
        title="Audit trail"
        subtitle={trail.data?.integrity_note}
        actions={
          trail.data && (
            <Pill tone={trail.data.chain.valid ? "green" : "red"} title="Recomputed SHA-256 hash chain over all events">
              {trail.data.chain.valid ? <ShieldCheck size={12} /> : <ShieldX size={12} />}{" "}
              {trail.data.chain.valid ? `chain intact · ${trail.data.chain.events} events` : `chain broken at #${trail.data.chain.broken_at}`}
            </Pill>
          )
        }
      >
        <div className="pp-filters">
          <label className="pp-inline-field">
            Event type
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              {KINDS.map((k) => <option key={k} value={k}>{k ? k.replace(".", "") : "All"}</option>)}
            </select>
          </label>
          <label className="pp-inline-field">
            Run ID
            <input value={runId} onChange={(e) => setRunId(e.target.value.trim())} placeholder="run_…" />
          </label>
        </div>
        <Async state={trail} isEmpty={(d) => !filter([...d.items, ...older]).length} empty="No events match.">
          {(d) => {
            const items = filter([...d.items, ...older]);
            const lastId = (older.length ? older : d.items).at(-1)?.id;
            return (
              <>
                <ol className="pp-timeline">
                  {items.map((e) => (
                    <li key={e.id}>
                      <div className="pp-tl-head">
                        <span className="pp-muted small">#{e.id} · {new Date(e.at).toLocaleString()}</span>
                        <Pill tone={tone(e.kind)}>{e.kind}</Pill>
                        <code className="small">{e.actor}</code>
                      </div>
                      <p>{e.summary}</p>
                      <p className="pp-muted small">
                        {e.run_id && <>run <Link href={`/pulse?run=${e.run_id}`}><code>{e.run_id}</code></Link> </>}
                        {e.action_id && <>· action <code>{e.action_id}</code> </>}
                        {e.execution_id && <>· execution <code>{e.execution_id}</code> </>}
                        · hash <Hash value={e.hash} />
                      </p>
                      {Object.keys(e.detail).length > 0 && <Disclosure summary="Detail"><JsonView value={e.detail} /></Disclosure>}
                    </li>
                  ))}
                </ol>
                {lastId && lastId > 1 && (
                  <button
                    className="pp-btn ghost small"
                    disabled={loadingMore}
                    onClick={async () => {
                      setLoadingMore(true);
                      try {
                        const more = await pulseApi.audit({ run_id: runId || undefined, before_id: lastId, limit: 100 });
                        setOlder((o) => [...o, ...more.items]);
                      } finally {
                        setLoadingMore(false);
                      }
                    }}
                  >
                    {loadingMore ? "Loading…" : "Load older events"}
                  </button>
                )}
              </>
            );
          }}
        </Async>
      </Panel>
    </div>
  );
}
