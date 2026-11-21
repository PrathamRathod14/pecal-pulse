"""LangGraph orchestration and run lifecycle.

Graph:
  orchestrator → customer_intelligence → (risk_detection ∥ opportunity_intelligence)
  → strategy → pulseguard → [approval_gate ⟲ interrupt/resume] → workflow_execution → audit

State is checkpointed in its own SQLite file (AsyncSqliteSaver), separate
from business records, so a run paused at the approval gate survives an API
restart and resumes when the last pending decision arrives.
"""

from __future__ import annotations

import asyncio
import json
import logging
import operator
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import agents, audit, guard, tools
from .auth import Actor
from .config import get_settings
from .db import connect

log = logging.getLogger("pulse.service")

SCENARIOS = {
    "standard": {},
    "gemini_unavailable": {"llm_unavailable": True},
    "n8n_unavailable": {"n8n_unavailable": True},
    "short_expiry": {"short_expiry": True},
    "prompt_injection": {"prompt_injection": True},
}


class PulseState(TypedDict, total=False):
    run_id: str
    snapshot_id: str
    customer_id: str
    objective: str
    started_by: str
    scenario: dict
    plan: list[str]
    customer: dict
    risk: dict
    opportunity: dict
    strategy: dict
    action_ids: list[str]
    gate_rounds: int
    executions: list[dict]
    errors: Annotated[list[dict], operator.add]


def _pending(run_id: str) -> list[str]:
    guard.expire_due()
    with connect() as conn:
        return [r["id"] for r in conn.execute(
            "SELECT id FROM actions WHERE run_id=? AND status='pending_approval'", (run_id,))]


async def approval_gate(state: dict) -> dict:
    """Human-in-the-loop pause. Re-executed from the top on every resume."""
    run_id = state["run_id"]
    pending = _pending(run_id)
    if pending:
        agents.set_stage(run_id, "approval_gate", "awaiting_approval")
        with connect() as conn:
            if not conn.execute("SELECT 1 FROM agent_steps WHERE run_id=? AND agent='approval_gate' AND status='waiting'", (run_id,)).fetchone():
                conn.execute("INSERT INTO agent_steps(run_id,agent,status,attempt,output,started_at) VALUES (?,?,?,?,?,?)",
                             (run_id, "approval_gate", "waiting", 1, json.dumps({"pending_action_ids": pending}), audit.now_iso()))
        interrupt({"pending_action_ids": pending})
    with connect() as conn:
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM actions WHERE run_id=? GROUP BY status", (run_id,))}
        conn.execute("UPDATE agent_steps SET status='completed', output=?, finished_at=? WHERE run_id=? AND agent='approval_gate' AND status='waiting'",
                     (json.dumps({"decisions": counts}), audit.now_iso(), run_id))
    agents.set_stage(run_id, "approval_gate", "running")
    return {"gate_rounds": (state.get("gate_rounds") or 0) + 1}


def _after_guard(state: dict) -> str:
    return "approval_gate" if _pending(state["run_id"]) else "workflow_execution"


def _after_gate(state: dict) -> str:
    return "approval_gate" if _pending(state["run_id"]) else "workflow_execution"


def build_graph():
    g = StateGraph(PulseState)
    g.add_node("orchestrator", agents.orchestrator)
    g.add_node("customer_intelligence", agents.customer_intelligence)
    g.add_node("risk_detection", agents.risk_detection)
    g.add_node("opportunity_intelligence", agents.opportunity_intelligence)
    g.add_node("strategy", agents.strategy)
    g.add_node("pulseguard", agents.pulseguard)
    g.add_node("approval_gate", approval_gate)
    g.add_node("workflow_execution", agents.workflow_execution)
    g.add_node("audit", agents.audit_agent)
    g.add_edge(START, "orchestrator")
    g.add_edge("orchestrator", "customer_intelligence")
    g.add_edge("customer_intelligence", "risk_detection")
    g.add_edge("customer_intelligence", "opportunity_intelligence")
    g.add_edge(["risk_detection", "opportunity_intelligence"], "strategy")
    g.add_edge("strategy", "pulseguard")
    g.add_conditional_edges("pulseguard", _after_guard, ["approval_gate", "workflow_execution"])
    g.add_conditional_edges("approval_gate", _after_gate, ["approval_gate", "workflow_execution"])
    g.add_edge("workflow_execution", "audit")
    g.add_edge("audit", END)
    return g


class Runtime:
    def __init__(self, graph, checkpointer):
        self.graph = graph
        self.checkpointer = checkpointer
        self.tasks: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def _lock(self, run_id: str) -> asyncio.Lock:
        return self.locks.setdefault(run_id, asyncio.Lock())

    async def start(self, actor: Actor, customer_id: str, objective: str, scenario: str = "standard") -> str:
        settings = get_settings()
        if scenario not in SCENARIOS:
            raise ValueError(f"Unknown scenario '{scenario}'")
        if scenario != "standard" and not settings.pulse_demo_mode:
            raise PermissionError("Demo scenarios are disabled (PULSE_DEMO_MODE=false)")
        sid = settings.snapshot_id
        if not tools.customer_exists(sid, customer_id):
            raise LookupError(f"Customer {customer_id} is not in snapshot {sid}")
        run_id = "run_" + uuid.uuid4().hex[:12]
        now = audit.now_iso()
        with connect() as conn:
            conn.execute(
                "INSERT INTO agent_runs(id,objective,customer_id,snapshot_id,started_by,scenario,status,stage,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, objective[:500], customer_id, sid, actor.id, scenario, "running", "queued", now, now))
            audit.append("run.started", actor.id, f"{actor.name} started an investigation of {customer_id}", run_id=run_id,
                         detail={"objective": objective[:500], "scenario": scenario}, conn=conn)
        state = {"run_id": run_id, "snapshot_id": sid, "customer_id": customer_id, "objective": objective,
                 "started_by": actor.id, "scenario": SCENARIOS[scenario], "errors": []}
        self._spawn(run_id, state)
        return run_id

    def _spawn(self, run_id: str, payload) -> None:
        task = asyncio.create_task(self._drive(run_id, payload))
        self.tasks[run_id] = task
        task.add_done_callback(lambda _t: self.tasks.pop(run_id, None))

    async def _drive(self, run_id: str, payload) -> None:
        config = {"configurable": {"thread_id": run_id}, "recursion_limit": 60}
        async with self._lock(run_id):
            try:
                while True:
                    await self.graph.ainvoke(payload, config)
                    snap = await self.graph.aget_state(config)
                    if not snap.next:
                        break
                    if _pending(run_id):  # paused at the approval gate
                        return
                    # Every decision arrived while the gate was pausing: resume now.
                    payload = Command(resume={"resumed_at": audit.now_iso()})
                self._finalize_after_graph(run_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("run %s failed: %s", run_id, type(exc).__name__)
                with connect() as conn:
                    conn.execute("UPDATE agent_runs SET status='failed', error=?, updated_at=? WHERE id=?",
                                 (f"{type(exc).__name__}: {exc}"[:500], audit.now_iso(), run_id))
                    audit.append("run.failed", "agent:orchestrator", f"Run failed: {type(exc).__name__}", run_id=run_id,
                                 detail={"error": str(exc)[:300]}, conn=conn)

    def _finalize_after_graph(self, run_id: str) -> None:
        with connect() as conn:
            conn.execute("UPDATE agent_runs SET stage='done', updated_at=? WHERE id=?", (audit.now_iso(), run_id))
        refresh_run_status(run_id)

    async def maybe_resume(self, run_id: str) -> bool:
        """Resume a paused run once no decision is pending."""
        if _pending(run_id):
            return False
        if run_id in self.tasks:
            return True  # the driver re-checks pending decisions after pausing and continues itself
        config = {"configurable": {"thread_id": run_id}}
        snap = await self.graph.aget_state(config)
        if not snap.next:
            return False
        audit.append("run.resumed", "agent:orchestrator", "All decisions recorded; resuming workflow", run_id=run_id)
        self._spawn(run_id, Command(resume={"resumed_at": audit.now_iso()}))
        return True

    async def retry_execution(self, actor: Actor, execution_id: str) -> dict:
        from . import n8n

        with connect() as conn:
            row = conn.execute("SELECT e.*, a.run_id FROM executions e JOIN actions a ON a.id=e.action_id WHERE e.id=?",
                               (execution_id,)).fetchone()
            run = conn.execute("SELECT scenario FROM agent_runs WHERE id=?", (row["run_id"],)).fetchone() if row else None
        if row is None:
            raise LookupError("Unknown execution")
        grant = guard.authorize_execution(row["action_id"], retry_execution_id=execution_id)
        audit.append("execution.retry", actor.id, f"{actor.name} retried execution", run_id=row["run_id"],
                     action_id=row["action_id"], execution_id=execution_id)
        outcome = await n8n.dispatch(grant, run_id=row["run_id"])  # retries always use the configured n8n
        refresh_run_status(row["run_id"])
        return outcome

    async def sweep(self) -> None:
        """Expire stale approvals and resume runs that no longer wait on anyone."""
        from . import gemini
        if gemini.needs_reprobe():
            asyncio.create_task(gemini.probe())  # never blocks the approval sweep
        touched = guard.expire_due()
        with connect() as conn:
            waiting = [r["id"] for r in conn.execute("SELECT id FROM agent_runs WHERE status='awaiting_approval'")]
        for run_id in set(touched) | set(waiting):
            try:
                await self.maybe_resume(run_id)
            except Exception:  # noqa: BLE001
                log.warning("resume of %s failed", run_id)


def refresh_run_status(run_id: str) -> str:
    """Derive the run's status from its actions and executions."""
    with connect() as conn:
        run = conn.execute("SELECT status, stage FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        if run is None or run["status"] == "failed":
            return run["status"] if run else "unknown"
        actions = conn.execute("SELECT status FROM actions WHERE run_id=?", (run_id,)).fetchall()
        execs = conn.execute("SELECT e.status FROM executions e JOIN actions a ON a.id=e.action_id WHERE a.run_id=?",
                             (run_id,)).fetchall()
        a_status = [r["status"] for r in actions]
        e_status = [r["status"] for r in execs]
        if "pending_approval" in a_status:
            status = "awaiting_approval"
        elif any(s in ("authorized", "dispatching", "submitted") for s in e_status):
            status = "awaiting_callback"
        elif "dispatch_failed" in e_status or "failed" in e_status:
            status = "completed_with_failures"
        else:
            status = "completed"
        graph_finished = run["stage"] in ("done", "awaiting_callback", "completed")
        if not graph_finished and status != "awaiting_approval":
            return run["status"]  # agents still working: the graph's own finalize decides completion
        if status != run["status"]:
            conn.execute("UPDATE agent_runs SET status=?, stage=?, updated_at=? WHERE id=?",
                         (status, "done" if status.startswith("completed") else status, audit.now_iso(), run_id))
            if status.startswith("completed"):
                audit.append("run.completed", "agent:audit", f"Run {status.replace('_', ' ')}", run_id=run_id,
                             detail={"actions": a_status, "executions": e_status}, conn=conn)
    return status


def handle_callback(body: bytes, timestamp: str | None, signature: str | None) -> dict:
    """n8n → backend execution result. Signed, deduplicated, idempotent."""
    guard.verify_webhook(body, timestamp, signature, source="service:n8n")
    try:
        msg = json.loads(body)
        delivery_id = str(msg["delivery_id"])
        execution_id = str(msg["execution_id"])
        status = msg["status"]
    except (ValueError, KeyError, TypeError):
        raise guard.GuardError("bad_payload", "Callback payload is malformed", 422)
    if status not in ("succeeded", "failed"):
        raise guard.GuardError("bad_payload", "status must be succeeded or failed", 422)
    with connect() as conn:
        execution = conn.execute("SELECT e.*, a.payload_hash, a.run_id, a.action_type FROM executions e JOIN actions a ON a.id=e.action_id WHERE e.id=?",
                                 (execution_id,)).fetchone()
        if execution is None:
            raise guard.GuardError("not_found", "Unknown execution", 404)
        if msg.get("idempotency_key") != execution["idempotency_key"] or msg.get("payload_hash") != execution["payload_hash"]:
            guard.security_event("callback_mismatch", "high", "service:n8n", execution_id,
                                 {"reason": "idempotency key or payload hash mismatch"}, conn=conn)
            raise guard.GuardError("mismatch", "Callback does not match the authorized execution", 409)
        if not guard.claim_delivery(delivery_id, "n8n", execution_id, conn):
            audit.append("callback.duplicate", "service:n8n", "Duplicate callback ignored", run_id=execution["run_id"],
                         action_id=execution["action_id"], execution_id=execution_id, detail={"delivery_id": delivery_id}, conn=conn)
            return {"accepted": True, "duplicate": True}
        if execution["status"] in ("succeeded", "failed"):
            audit.append("callback.ignored", "service:n8n", "Execution already final; callback not applied twice",
                         run_id=execution["run_id"], action_id=execution["action_id"], execution_id=execution_id, conn=conn)
            return {"accepted": True, "duplicate": True}
        result = msg.get("result") if isinstance(msg.get("result"), dict) else {}
        error = str(msg.get("error") or "")[:500] or None
        now = audit.now_iso()
        conn.execute("UPDATE executions SET status=?, result=?, error=?, n8n_execution_id=COALESCE(?, n8n_execution_id), completed_at=?, updated_at=? WHERE id=?",
                     (status, json.dumps(result, ensure_ascii=False)[:20000], error, msg.get("n8n_execution_id"), now, now, execution_id))
        conn.execute("UPDATE actions SET status=?, updated_at=? WHERE id=?",
                     ("executed" if status == "succeeded" else "execution_failed", now, execution["action_id"]))
        delivery = result.get("delivery")
        audit.append(f"execution.{status}", "service:n8n",
                     f"n8n reported {execution['action_type']} {status}" + (f" (delivery: {delivery})" if delivery else ""),
                     run_id=execution["run_id"], action_id=execution["action_id"], execution_id=execution_id,
                     detail={"n8n_execution_id": msg.get("n8n_execution_id"), "result": result, "error": error}, conn=conn)
        run_id = execution["run_id"]
    refresh_run_status(run_id)
    return {"accepted": True, "duplicate": False}


_runtime: Runtime | None = None


def runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("Pulse runtime is not started")
    return _runtime


@asynccontextmanager
async def pulse_lifespan():
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    global _runtime
    settings = get_settings()
    settings.checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        from .demo_data import SNAPSHOT_ID, write
        if settings.snapshot_id == SNAPSHOT_ID:
            write()
    except Exception:  # noqa: BLE001
        log.warning("demo snapshot generation skipped")
    with connect() as conn:  # runs that were mid-flight when the process died
        conn.execute("UPDATE agent_runs SET status='failed', error='Interrupted by API restart', updated_at=? WHERE status='running'",
                     (audit.now_iso(),))
    probe_task = None
    if settings.gemini_configured:
        from . import gemini

        async def _probe():
            try:
                result = await gemini.probe()
                log.warning("Gemini probe: %s", {k: v for k, v in result.items() if k != "reason"} if result.get("ok") else result.get("reason"))
            except Exception:  # noqa: BLE001
                log.warning("Gemini probe failed")

        probe_task = asyncio.create_task(_probe())  # startup never waits on Google
    async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_file)) as saver:
        _runtime = Runtime(build_graph().compile(checkpointer=saver), saver)

        async def sweeper():
            while True:
                await asyncio.sleep(10)
                try:
                    await _runtime.sweep()
                except Exception:  # noqa: BLE001
                    log.warning("sweeper iteration failed")

        task = asyncio.create_task(sweeper())
        try:
            yield _runtime
        finally:
            task.cancel()
            if probe_task:
                probe_task.cancel()
            for t in list(_runtime.tasks.values()):
                t.cancel()
            _runtime = None
