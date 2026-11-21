"use client";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { AlertTriangle, Inbox, Loader2, RefreshCw } from "lucide-react";
import type { PulseError } from "./api";
import { useNow } from "./hooks";

type Tone = "neutral" | "green" | "orange" | "red" | "purple" | "blue";

const STATUS_TONE: Record<string, Tone> = {
  completed: "green",
  succeeded: "green",
  executed: "green",
  approved: "green",
  auto_approved: "green",
  allow: "green",
  running: "blue",
  executing: "blue",
  submitted: "blue",
  dispatching: "blue",
  authorized: "blue",
  awaiting_callback: "blue",
  waiting: "orange",
  awaiting_approval: "orange",
  pending_approval: "orange",
  require_approval: "orange",
  retrying: "orange",
  expired: "neutral",
  probe_closed: "neutral",
  probe_not_dispatched: "neutral",
  rejected: "red",
  blocked: "red",
  deny: "red",
  failed: "red",
  dispatch_failed: "red",
  execution_failed: "red",
  completed_with_failures: "red",
  critical: "red",
  high: "orange",
  medium: "purple",
  low: "green",
  opportunity: "green",
};

export function Pill({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`pp-pill ${tone}`} title={title}>
      {children}
    </span>
  );
}

export function StatusPill({ value, title }: { value: string | null | undefined; title?: string }) {
  if (!value) return null;
  return (
    <Pill tone={STATUS_TONE[value] ?? "neutral"} title={title}>
      {value.replace(/_/g, " ")}
    </Pill>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="pp-state" role="status">
      <Loader2 size={18} className="pp-spin" aria-hidden />
      <span>{label}</span>
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: PulseError | Error | string; onRetry?: () => void }) {
  const message = typeof error === "string" ? error : error.message;
  return (
    <div className="pp-state error" role="alert">
      <AlertTriangle size={18} aria-hidden />
      <span>{message}</span>
      {onRetry && (
        <button className="pp-btn ghost small" onClick={onRetry}>
          <RefreshCw size={13} /> Retry
        </button>
      )}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="pp-state empty">
      <Inbox size={18} aria-hidden />
      <span>{children}</span>
    </div>
  );
}

/** Loading / error / empty wrapper that keeps stale data visible on refresh errors. */
export function Async<T>({
  state,
  empty,
  isEmpty,
  children,
}: {
  state: { data: T | null; error: PulseError | null; loading: boolean; reload: () => void };
  empty?: ReactNode;
  isEmpty?: (data: T) => boolean;
  children: (data: T) => ReactNode;
}) {
  if (state.data === null) {
    if (state.error) return <ErrorState error={state.error} onRetry={state.reload} />;
    return <Loading />;
  }
  return (
    <>
      {state.error && <ErrorState error={`Showing last loaded data — ${state.error.message}`} onRetry={state.reload} />}
      {isEmpty?.(state.data) ? <Empty>{empty}</Empty> : children(state.data)}
    </>
  );
}

export function Panel({
  title,
  subtitle,
  actions,
  children,
  className = "",
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`pp-panel ${className}`}>
      <header className="pp-panel-head">
        <div>
          <h2>{title}</h2>
          {subtitle && <p>{subtitle}</p>}
        </div>
        {actions && <div className="pp-panel-actions">{actions}</div>}
      </header>
      {children}
    </section>
  );
}

export function Kpi({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: string; tone?: Tone }) {
  return (
    <div className={`pp-kpi ${tone ?? ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {hint && <small>{hint}</small>}
    </div>
  );
}

export function timeAgo(iso: string | null | undefined, now = Date.now()) {
  if (!iso) return "—";
  const s = Math.round((now - new Date(iso).getTime()) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}

export function TimeAgo({ iso }: { iso: string | null | undefined }) {
  const now = useNow(15000);
  return (
    <time dateTime={iso ?? undefined} title={iso ? new Date(iso).toLocaleString() : undefined}>
      {timeAgo(iso, now)}
    </time>
  );
}

export function Countdown({ iso }: { iso: string | null }) {
  const now = useNow(1000);
  if (!iso) return null;
  const left = Math.round((new Date(iso).getTime() - now) / 1000);
  if (left <= 0) return <Pill tone="neutral">expired</Pill>;
  const m = Math.floor(left / 60),
    s = left % 60;
  return (
    <Pill tone={left < 60 ? "red" : "orange"} title={`Expires ${new Date(iso).toLocaleString()}`}>
      expires in {m}:{String(s).padStart(2, "0")}
    </Pill>
  );
}

export function JsonView({ value }: { value: unknown }) {
  return <pre className="pp-json">{JSON.stringify(value, null, 2)}</pre>;
}

export function Hash({ value }: { value: string | null | undefined }) {
  if (!value) return null;
  return (
    <code className="pp-hash" title={value}>
      {value.slice(0, 10)}…
    </code>
  );
}

/** Small dependency-free SVG bar sparkline (accessible: table fallback via title). */
export function Sparkline({
  series,
  height = 44,
  highlightLast = 3,
}: {
  series: { month: string; calibrations: number }[];
  height?: number;
  highlightLast?: number;
}) {
  if (!series.length) return null;
  const max = Math.max(1, ...series.map((p) => p.calibrations));
  const w = 8,
    gap = 3;
  const width = series.length * (w + gap);
  return (
    <svg
      className="pp-spark"
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`Monthly calibrations: ${series.map((p) => `${p.month} ${p.calibrations}`).join(", ")}`}
    >
      {series.map((p, i) => {
        const h = Math.max(2, (p.calibrations / max) * (height - 4));
        const recent = i >= series.length - highlightLast;
        return (
          <rect
            key={p.month}
            x={i * (w + gap)}
            y={height - h}
            width={w}
            height={h}
            rx={2}
            className={recent ? "recent" : ""}
          >
            <title>
              {p.month}: {p.calibrations}
            </title>
          </rect>
        );
      })}
    </svg>
  );
}

export function Disclosure({ summary, children, defaultOpen = false }: { summary: ReactNode; children: ReactNode; defaultOpen?: boolean }) {
  return (
    <details className="pp-disclosure" open={defaultOpen}>
      <summary>{summary}</summary>
      <div>{children}</div>
    </details>
  );
}

/** Briefly highlight content when its key changes (respects reduced motion via CSS). */
export function useFlash(key: unknown) {
  const [flash, setFlash] = useState(false);
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    setFlash(true);
    const t = setTimeout(() => setFlash(false), 900);
    return () => clearTimeout(t);
  }, [key]);
  return flash;
}

export const AGENT_LABELS: Record<string, string> = {
  orchestrator: "Orchestrator",
  customer_intelligence: "Customer Intelligence",
  risk_detection: "Risk Detection",
  opportunity_intelligence: "Opportunity Intelligence",
  strategy: "Strategy",
  pulseguard: "PulseGuard",
  approval_gate: "Human approval",
  workflow_execution: "Workflow Execution",
  audit: "Audit",
};

export const SCENARIO_LABELS: Record<string, string> = {
  standard: "Standard investigation",
  gemini_unavailable: "Gemini unavailable",
  n8n_unavailable: "n8n outage",
  short_expiry: "Short approval expiry (45 s)",
  prompt_injection: "Prompt-injection red team",
};
