"use client";
// Small Pulse entry points embedded in the existing Dashboard and Customers pages.
import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Bot } from "lucide-react";
import type { Overview, PulseActor } from "@/types/pulse";
import { pulseApi } from "./api";

export function PulseDashboardStrip() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [state, setState] = useState<"loading" | "ok" | "signed_out" | "error">("loading");
  useEffect(() => {
    let alive = true;
    const load = () =>
      pulseApi
        .overview()
        .then((o) => alive && (setOverview(o), setState("ok")))
        .catch((e) => alive && setState(e.status === 401 ? "signed_out" : "error"));
    load();
    const t = setInterval(load, 15000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);
  return (
    <div className="pp-strip" aria-live="polite">
      <Bot size={16} aria-hidden />
      <strong>Agent Ops</strong>
      {state === "loading" && <span className="pp-muted small">Loading agent activity…</span>}
      {state === "signed_out" && <span className="pp-muted small">Sign in to Agent Ops to see signals, agent runs and approvals.</span>}
      {state === "error" && <span className="pp-muted small">Agent services unavailable right now.</span>}
      {state === "ok" && overview && (
        <span className="pp-strip-stats">
          <span><b>{overview.signals.critical}</b> critical signals</span>
          <span><b>{overview.pending_approvals}</b> pending approvals</span>
          <span><b>{(overview.runs.running ?? 0) + (overview.runs.awaiting_approval ?? 0) + (overview.runs.awaiting_callback ?? 0)}</b> active runs</span>
          <span><b>{overview.policy_outcomes.deny ?? 0}</b> actions blocked by PulseGuard</span>
        </span>
      )}
      <Link href={state === "ok" && overview?.pending_approvals ? "/pulse/approvals" : "/pulse"}>
        {state === "ok" && overview?.pending_approvals ? "Review approvals →" : "Open Agent Ops →"}
      </Link>
    </div>
  );
}

export function InvestigateButton({ customerId }: { customerId: string }) {
  const router = useRouter();
  const [actor, setActor] = useState<PulseActor | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    pulseApi.me().then((r) => setActor(r.actor)).catch(() => setActor(null));
  }, []);
  const allowed = !!actor?.permissions.includes("investigation:start");
  const title =
    actor === null
      ? "Sign in to Agent Ops first"
      : !allowed
        ? "Your Pulse role cannot start investigations"
        : "Agents investigate this account and propose actions for approval";
  return (
    <span className="pp-investigate">
      <button
        className="pp-btn ghost small"
        disabled={busy || actor === undefined || (actor !== null && !allowed)}
        title={title}
        onClick={async () => {
          if (actor === null) return router.push("/pulse");
          setBusy(true);
          setError(null);
          try {
            const r = await pulseApi.startRun(customerId, "standard");
            router.push(`/pulse?run=${r.run_id}`);
          } catch (e) {
            setError((e as Error).message);
            setBusy(false);
          }
        }}
      >
        <Bot size={13} /> {busy ? "Starting…" : actor === null ? "Sign in to investigate" : "Investigate with Pulse"}
      </button>
      {error && <span className="pp-error-text small" role="alert"> {error}</span>}
    </span>
  );
}
