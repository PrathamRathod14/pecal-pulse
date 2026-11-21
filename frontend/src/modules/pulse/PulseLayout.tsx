"use client";
import { useEffect, useState, type ReactNode } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Bot,
  CheckSquare,
  Workflow,
  ShieldCheck,
  ScrollText,
  LogOut,
  UserRound,
  Sparkles,
  Webhook,
  Mail,
} from "lucide-react";
import type { PulseActor } from "@/types/pulse";
import { pulseApi } from "./api";
import { PulseSessionProvider, usePoll, usePulseSession } from "./hooks";
import { ErrorState, Loading, Pill } from "./ui";

const tabs = [
  { href: "/pulse", label: "Agent Control Center", icon: Bot },
  { href: "/pulse/approvals", label: "Approvals", icon: CheckSquare, badge: true },
  { href: "/pulse/workflows", label: "Workflows", icon: Workflow },
  { href: "/pulse/security", label: "PulseGuard", icon: ShieldCheck },
  { href: "/pulse/audit", label: "Audit trail", icon: ScrollText },
];

const ROLE_LABEL: Record<string, string> = {
  viewer: "Viewer",
  analyst: "Analyst",
  sales_manager: "Sales manager",
  director: "Director",
};

export function roleLabel(role: string) {
  return ROLE_LABEL[role] ?? role;
}

export function PulseLayout({ children }: { children: ReactNode }) {
  return (
    <PulseSessionProvider>
      <Shell>{children}</Shell>
    </PulseSessionProvider>
  );
}

function Shell({ children }: { children: ReactNode }) {
  const { actor, ready } = usePulseSession();
  if (!ready) return <Loading label="Connecting to SignalPilot…" />;
  return (
    <div className="pp-root">
      <Header />
      {actor ? children : <SignIn />}
    </div>
  );
}

function Header() {
  const pathname = usePathname();
  const { actor, status, statusError, signOut } = usePulseSession();
  const pending = usePoll(
    () => (actor ? pulseApi.actions("pending_approval").then((r) => r.items.length) : Promise.resolve(0)),
    8000,
    [actor?.id],
  );
  return (
    <header className="pp-header">
      <div className="pp-title">
        <div>
          <h1>Agent Ops</h1>
          <p>Agents investigate · PulseGuard enforces · people approve · n8n executes</p>
        </div>
        <div className="pp-header-side">
          <ServiceChips />
          {actor && <IdentityMenu actor={actor} onSignOut={signOut} />}
        </div>
      </div>
      {statusError && <ErrorState error={`Status unavailable: ${statusError}`} />}
      {status?.snapshot_id.startsWith("synthetic") && (
        <p className="pp-demo-note">
          <Sparkles size={13} aria-hidden /> Demo dataset <code>{status.snapshot_id}</code>: all customers, contacts and
          notes are synthetic. Values are calibration volumes — the source contains no revenue.
        </p>
      )}
      <nav className="pp-tabs" aria-label="Agent Ops sections">
        {tabs.map((t) => {
          const active = t.href === "/pulse" ? pathname === "/pulse" : pathname.startsWith(t.href);
          return (
            <Link key={t.href} href={t.href} className={active ? "active" : ""} aria-current={active ? "page" : undefined}>
              <t.icon size={15} aria-hidden />
              {t.label}
              {t.badge && !!pending.data && <i aria-label={`${pending.data} pending`}>{pending.data}</i>}
            </Link>
          );
        })}
      </nav>
    </header>
  );
}

function ServiceChips() {
  const { status } = usePulseSession();
  if (!status) return null;
  return (
    <div className="pp-chips" aria-label="Service status">
      <Pill
        tone={status.gemini.verified ? "green" : status.gemini.configured ? "red" : "orange"}
        title={
          status.gemini.verified
            ? `Gemini ${status.gemini.model} verified (${status.gemini.mode === "vertex" ? "Vertex AI express" : "Gemini API"})`
            : status.gemini.configured
              ? status.gemini.problem ?? "Key set but not verified yet"
              : `${status.gemini.missing_env} not set — Strategy uses the labelled deterministic fallback`
        }
      >
        <Sparkles size={12} /> Gemini{" "}
        {status.gemini.verified ? status.gemini.model : status.gemini.configured ? "key not working" : "not configured"}
      </Pill>
      <Pill
        tone={status.n8n.reachable ? "green" : "red"}
        title={status.n8n.reachable ? `n8n at ${status.n8n.url}` : `n8n unreachable at ${status.n8n.url}`}
      >
        <Webhook size={12} /> n8n {status.n8n.reachable ? "online" : "offline"}
      </Pill>
      <Pill tone={status.email.external_delivery ? "orange" : "neutral"} title="Customer e-mail delivery mode">
        <Mail size={12} /> E-mail: {status.email.mode}
      </Pill>
    </div>
  );
}

function IdentityMenu({ actor, onSignOut }: { actor: PulseActor; onSignOut: () => void }) {
  const { signIn, status } = usePulseSession();
  const [users, setUsers] = useState<PulseActor[]>([]);
  useEffect(() => {
    pulseApi.users().then((r) => setUsers(r.users)).catch(() => undefined);
  }, []);
  return (
    <div className="pp-identity">
      <UserRound size={15} aria-hidden />
      {status?.auth_mode === "demo" && users.length ? (
        <label>
          <span className="pp-sr">Signed in as</span>
          <select
            value={actor.id}
            onChange={(e) => signIn(e.target.value).catch(() => undefined)}
            title="Demo identities — the role is resolved by the server, not the browser"
          >
            {users.map((u) => (
              <option key={u.id} value={u.id}>
                {u.name} · {roleLabel(u.role)}
              </option>
            ))}
          </select>
        </label>
      ) : (
        <span>
          {actor.name} · {roleLabel(actor.role)}
        </span>
      )}
      <button className="pp-icon-btn" onClick={onSignOut} aria-label="Sign out" title="Sign out">
        <LogOut size={14} />
      </button>
    </div>
  );
}

function SignIn() {
  const { signIn, status } = usePulseSession();
  const [users, setUsers] = useState<PulseActor[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  useEffect(() => {
    pulseApi
      .users()
      .then((r) => setUsers(r.users))
      .catch((e) => setError(e.message));
  }, []);
  const passwordMode = status?.auth_mode === "password";
  return (
    <section className="pp-panel pp-signin">
      <h2>Sign in to SignalPilot Agent Ops</h2>
      <p>
        Approvals and policy decisions are bound to a server-side identity.{" "}
        {passwordMode
          ? "Enter your password."
          : "Local demo mode: choose a demo identity (no password). Roles are assigned by the server."}
      </p>
      {error && <ErrorState error={error} />}
      {!users && !error && <Loading />}
      {passwordMode && (
        <label className="pp-field">
          Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
        </label>
      )}
      <div className="pp-user-grid">
        {users?.map((u) => (
          <button
            key={u.id}
            className="pp-user"
            disabled={!!busy}
            onClick={async () => {
              setBusy(u.id);
              setError(null);
              try {
                await signIn(u.id, passwordMode ? password : undefined);
              } catch (e) {
                setError((e as Error).message);
              } finally {
                setBusy(null);
              }
            }}
          >
            <strong>{u.name}</strong>
            <span>{roleLabel(u.role)}</span>
            <small>{describeRole(u.role)}</small>
          </button>
        ))}
      </div>
    </section>
  );
}

function describeRole(role: string) {
  switch (role) {
    case "analyst":
      return "Starts investigations; cannot approve actions.";
    case "sales_manager":
      return "Approves standard actions (e-mails, reviews); retries executions.";
    case "director":
      return "Also approves financial actions; runs security probes.";
    default:
      return "Read-only access.";
  }
}
