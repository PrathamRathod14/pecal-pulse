"use client";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { PulseActor, PulseStatus } from "@/types/pulse";
import { pulseApi, PulseError } from "./api";

/** Poll an endpoint; keeps the last good data visible while refreshing. */
export function usePoll<T>(fetcher: () => Promise<T>, intervalMs = 0, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<PulseError | null>(null);
  const [loading, setLoading] = useState(true);
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;
  const depsKey = JSON.stringify(deps);
  const lastKey = useRef(depsKey);
  if (lastKey.current !== depsKey) {
    // Different subject (e.g. another run): never show the previous subject's data.
    lastKey.current = depsKey;
    setData(null);
    setError(null);
  }
  const reload = useCallback(async () => {
    const key = lastKey.current;
    try {
      const next = await fetchRef.current();
      if (key !== lastKey.current) return; // response for a previous subject
      setData(next);
      setError(null);
    } catch (e) {
      if (key !== lastKey.current) return;
      setError(e instanceof PulseError ? e : new PulseError(String(e), 0));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    reload();
    if (!intervalMs) return;
    const timer = setInterval(() => {
      if (alive && document.visibilityState === "visible") reload();
    }, intervalMs);
    return () => {
      alive = false;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, reload, ...deps]);
  return { data, error, loading, reload };
}

interface Session {
  actor: PulseActor | null;
  status: PulseStatus | null;
  statusError: string | null;
  ready: boolean;
  can: (permission: string) => boolean;
  signIn: (userId: string, password?: string) => Promise<void>;
  signOut: () => Promise<void>;
  refreshStatus: () => Promise<void>;
}

const SessionContext = createContext<Session | null>(null);

export function PulseSessionProvider({ children }: { children: ReactNode }) {
  const [actor, setActor] = useState<PulseActor | null>(null);
  const [status, setStatus] = useState<PulseStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await pulseApi.status());
      setStatusError(null);
    } catch (e) {
      setStatusError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    Promise.all([
      pulseApi.me().then((r) => setActor(r.actor)).catch(() => setActor(null)),
      refreshStatus(),
    ]).finally(() => setReady(true));
    const timer = setInterval(refreshStatus, 20000);
    return () => clearInterval(timer);
  }, [refreshStatus]);
  const value: Session = {
    actor,
    status,
    statusError,
    ready,
    can: (p) => !!actor?.permissions.includes(p),
    signIn: async (userId, password) => {
      const r = await pulseApi.login(userId, password);
      setActor(r.actor);
    },
    signOut: async () => {
      await pulseApi.logout().catch(() => undefined);
      setActor(null);
    },
    refreshStatus,
  };
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function usePulseSession(): Session {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("usePulseSession must be used inside PulseSessionProvider");
  return ctx;
}

/** Re-render every second (for countdowns). */
export function useNow(intervalMs = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}
