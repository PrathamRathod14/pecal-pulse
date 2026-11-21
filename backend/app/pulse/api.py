"""PeCal Pulse agent-workflow API, mounted at /api/pulse.

Humans authenticate with a signed session cookie; mutating routes also
require the X-Pulse-Request header (CSRF). n8n authenticates with HMAC
signatures. Roles come from the server-side directory only.
"""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from . import audit, auth, guard, service, signals, tools
from .actions import REGISTRY
from .auth import Actor, current_actor, require, require_csrf
from .config import get_settings
from .db import connect, schema_version

router = APIRouter(prefix="/api/pulse")


def _guard_http(exc: guard.GuardError) -> HTTPException:
    return HTTPException(exc.status, {"code": exc.code, "message": exc.message})


def _row(r) -> dict:
    d = dict(r)
    for key in ("payload", "policy_outcome", "output", "tool_calls", "result", "rules", "reasons", "detail"):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except ValueError:
                pass
    return d


# ---------------------------------------------------------------- status & auth

@router.get("/status")
async def status():
    from . import n8n

    settings = get_settings()
    n8n_health = await n8n.health()
    return {
        "snapshot_id": settings.snapshot_id,
        "demo_mode": settings.pulse_demo_mode,
        "auth_mode": settings.pulse_auth_mode,
        "schema_version": schema_version(),
        "gemini": {"configured": settings.gemini_configured, "model": settings.gemini_model,
                   "missing_env": None if settings.gemini_configured else "GEMINI_API_KEY"},
        "n8n": {**n8n_health, "webhook_secret_configured": bool(settings.pulse_webhook_secret.get_secret_value())},
        "email": {"external_delivery": settings.allow_external_email,
                  "mode": "external (via n8n SMTP)" if settings.allow_external_email else "simulated preview"},
        "approval_ttl_seconds": settings.approval_ttl_seconds,
        "auto_approve_low_risk": settings.auto_approve_low_risk,
        "scenarios": list(service.SCENARIOS) if settings.pulse_demo_mode else ["standard"],
    }


class LoginRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=60)
    password: str | None = Field(default=None, max_length=200)


@router.get("/auth/users")
def users():
    return {"auth_mode": get_settings().pulse_auth_mode, "users": auth.list_users()}


@router.post("/auth/login")
def login(body: LoginRequest, response: Response, request: Request):
    require_csrf(request)
    if not auth.check_password(body.user_id, body.password):
        guard.security_event("login_failed", "warning", body.user_id[:60], None, {"mode": get_settings().pulse_auth_mode})
        raise HTTPException(401, "Unknown user or wrong password")
    response.set_cookie(auth.SESSION_COOKIE, auth.issue_session(body.user_id), httponly=True, samesite="strict",
                        max_age=auth.SESSION_TTL, secure=request.url.scheme == "https", path="/")
    actor = auth.read_session(auth.issue_session(body.user_id))
    audit.append("auth.login", actor.id, f"{actor.name} signed in ({actor.role})")
    return {"actor": actor.public()}


@router.post("/auth/logout")
def logout(response: Response, request: Request):
    require_csrf(request)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
def me(request: Request):
    actor = auth.read_session(request.cookies.get(auth.SESSION_COOKIE))
    return {"actor": actor.public() if actor else None, "auth_mode": get_settings().pulse_auth_mode}


# ---------------------------------------------------------------- overview & signals

@router.get("/signals")
def list_signals(actor: Actor = Depends(current_actor), limit: int = Query(30, ge=1, le=200)):
    require(actor, "pulse:read")
    sid = get_settings().snapshot_id
    items = signals.scan(sid, limit)
    latest: dict[str, dict] = {}
    with connect() as conn:
        for r in conn.execute("SELECT customer_id, id, status, created_at FROM agent_runs WHERE scenario IS NOT 'security_probe' ORDER BY created_at"):
            latest[r["customer_id"]] = dict(r)
    for item in items:
        item["latest_run"] = latest.get(item["customer_id"])
    return {"snapshot_id": sid, "mode": "mock" if sid.startswith("synthetic") else "historical",
            "basis": "Measured from monthly calibration history; value = calibration volume (no revenue in source).",
            "items": items}


@router.get("/overview")
def overview(actor: Actor = Depends(current_actor)):
    require(actor, "pulse:read")
    sid = get_settings().snapshot_id
    guard.expire_due()
    sigs = signals.scan(sid, 500)
    with connect() as conn:
        runs = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM agent_runs GROUP BY status")}
        actions = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM actions GROUP BY status")}
        execs = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM executions GROUP BY status")}
        decisions = {r["outcome"]: r["n"] for r in conn.execute("SELECT outcome, COUNT(*) n FROM policy_decisions WHERE phase='proposal' GROUP BY outcome")}
        recent = [_row(r) for r in conn.execute(
            "SELECT id, kind, actor, summary, at, run_id, action_id FROM audit_events ORDER BY id DESC LIMIT 8")]
        active = [_row(r) for r in conn.execute(
            "SELECT id, customer_id, status, stage, llm_source, created_at FROM agent_runs ORDER BY created_at DESC LIMIT 6")]
    at_risk = [s for s in sigs if s["severity"] in ("critical", "high")]
    return {
        "snapshot_id": sid,
        "signals": {"critical": sum(1 for s in sigs if s["severity"] == "critical"),
                    "high": sum(1 for s in sigs if s["severity"] == "high"),
                    "medium": sum(1 for s in sigs if s["severity"] == "medium"),
                    "opportunity": sum(1 for s in sigs if s["severity"] == "opportunity"),
                    "volume_at_risk_12m": sum(round(s["avg_prior_12m"] * 12 - s["avg_last_3m"] * 12) for s in at_risk if s["avg_prior_12m"]),
                    "volume_basis": "Calibrations/year implied by the gap between prior-12-month and last-3-month pace for critical/high signals."},
        "runs": runs, "actions": actions, "executions": execs, "policy_outcomes": decisions,
        "pending_approvals": actions.get("pending_approval", 0),
        "recent_events": recent, "recent_runs": active,
    }


# ---------------------------------------------------------------- runs

class StartRun(BaseModel):
    customer_id: str = Field(min_length=1, max_length=40)
    objective: str = Field(default="Investigate the business signal and recommend retention actions", max_length=500)
    scenario: Literal["standard", "gemini_unavailable", "n8n_unavailable", "short_expiry", "prompt_injection"] = "standard"


@router.post("/runs", status_code=202)
async def start_run(body: StartRun, request: Request, actor: Actor = Depends(current_actor)):
    require_csrf(request)
    require(actor, "investigation:start")
    try:
        run_id = await service.runtime().start(actor, body.customer_id, body.objective, body.scenario)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    return {"run_id": run_id}


@router.get("/runs")
def list_runs(actor: Actor = Depends(current_actor), limit: int = Query(30, ge=1, le=200)):
    require(actor, "pulse:read")
    with connect() as conn:
        rows = conn.execute("SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        counts = {}
        for r in conn.execute("SELECT run_id, status, COUNT(*) n FROM actions GROUP BY run_id, status"):
            counts.setdefault(r["run_id"], {})[r["status"]] = r["n"]
    return {"items": [{**_row(r), "action_counts": counts.get(r["id"], {})} for r in rows]}


@router.get("/runs/{run_id}")
def get_run(run_id: str, actor: Actor = Depends(current_actor)):
    require(actor, "pulse:read")
    with connect() as conn:
        run = conn.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise HTTPException(404, "Unknown run")
        steps = [_row(r) for r in conn.execute("SELECT * FROM agent_steps WHERE run_id=? ORDER BY id", (run_id,))]
        actions = [_action(conn, r) for r in conn.execute("SELECT * FROM actions WHERE run_id=? ORDER BY created_at", (run_id,))]
    return {"run": _row(run), "steps": steps, "actions": actions}


def _action(conn, r) -> dict:
    d = _row(r)
    spec = REGISTRY.get(d["action_type"])
    d["label"] = spec.label if spec else d["action_type"]
    d["external"] = spec.external if spec else None
    approval = conn.execute("SELECT * FROM approvals WHERE action_id=?", (d["id"],)).fetchone()
    d["approval"] = _row(approval) if approval else None
    execution = conn.execute("SELECT * FROM executions WHERE action_id=?", (d["id"],)).fetchone()
    d["execution"] = _row(execution) if execution else None
    d["decisions"] = [_row(x) for x in conn.execute("SELECT * FROM policy_decisions WHERE action_id=? ORDER BY id", (d["id"],))]
    return d


# ---------------------------------------------------------------- approvals

@router.get("/actions")
def list_actions(actor: Actor = Depends(current_actor), status: str | None = None, limit: int = Query(50, ge=1, le=200)):
    require(actor, "pulse:read")
    guard.expire_due()
    with connect() as conn:
        if status:
            rows = conn.execute("SELECT * FROM actions WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM actions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        items = [_action(conn, r) for r in rows]
        runs = {r["id"]: _row(r) for r in conn.execute(
            f"SELECT id, customer_id, summary, llm_source, started_by FROM agent_runs WHERE id IN ({','.join('?' * len(items)) or 'NULL'})",
            [i["run_id"] for i in items])}
    for item in items:
        item["run"] = runs.get(item["run_id"])
        item["can_decide"] = bool(item["status"] == "pending_approval" and _may_decide(actor, item))
    return {"items": items}


def _may_decide(actor: Actor, item: dict) -> bool:
    permission = (item.get("policy_outcome") or {}).get("approval_permission")
    return bool(permission and actor.can(permission)
                and auth.ROLE_RANK.get(actor.role, -1) >= auth.ROLE_RANK.get(item.get("required_role") or "sales_manager", 99))


@router.get("/actions/{action_id}")
def get_action(action_id: str, actor: Actor = Depends(current_actor)):
    require(actor, "pulse:read")
    with connect() as conn:
        r = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if r is None:
            raise HTTPException(404, "Unknown action")
        item = _action(conn, r)
    item["can_decide"] = bool(item["status"] == "pending_approval" and _may_decide(actor, item))
    return item


class Decide(BaseModel):
    payload_hash: str | None = Field(default=None, max_length=64)
    comment: str = Field(default="", max_length=500)


@router.post("/actions/{action_id}/approve")
async def approve(action_id: str, body: Decide, request: Request, actor: Actor = Depends(current_actor)):
    require_csrf(request)
    try:
        result = guard.approve(actor, action_id, body.payload_hash or "", body.comment)
    except guard.GuardError as exc:
        raise _guard_http(exc)
    result["resumed"] = await service.runtime().maybe_resume(result["run_id"])
    return result


@router.post("/actions/{action_id}/reject")
async def reject(action_id: str, body: Decide, request: Request, actor: Actor = Depends(current_actor)):
    require_csrf(request)
    try:
        result = guard.reject(actor, action_id, body.comment)
    except guard.GuardError as exc:
        raise _guard_http(exc)
    result["resumed"] = await service.runtime().maybe_resume(result["run_id"])
    return result


# ---------------------------------------------------------------- executions & n8n

@router.get("/executions")
def list_executions(actor: Actor = Depends(current_actor), limit: int = Query(50, ge=1, le=200)):
    require(actor, "pulse:read")
    with connect() as conn:
        rows = conn.execute(
            "SELECT e.*, a.action_type, a.customer_id, a.run_id FROM executions e JOIN actions a ON a.id=e.action_id"
            " ORDER BY e.created_at DESC LIMIT ?", (limit,)).fetchall()
    return {"items": [_row(r) for r in rows]}


@router.post("/executions/{execution_id}/retry")
async def retry_execution(execution_id: str, request: Request, actor: Actor = Depends(current_actor)):
    require_csrf(request)
    require(actor, "execution:retry")
    try:
        return await service.runtime().retry_execution(actor, execution_id)
    except guard.GuardError as exc:
        raise _guard_http(exc)
    except LookupError as exc:
        raise HTTPException(404, str(exc))


@router.get("/workflows")
async def workflows(actor: Actor = Depends(current_actor)):
    require(actor, "pulse:read")
    from . import n8n
    from .n8n_workflows import WORKFLOWS

    settings = get_settings()
    with connect() as conn:
        stats = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM executions GROUP BY status")}
        failures = [_row(r) for r in conn.execute(
            "SELECT e.id, e.status, e.error, e.attempts, e.updated_at, a.action_type, a.customer_id FROM executions e"
            " JOIN actions a ON a.id=e.action_id WHERE e.status IN ('dispatch_failed','failed') ORDER BY e.updated_at DESC LIMIT 20")]
    return {"n8n": await n8n.health(), "execute_webhook": settings.n8n_execute_path,
            "callback_url": settings.pulse_callback_base_url.rstrip("/") + "/api/pulse/n8n/callback",
            "workflows": WORKFLOWS, "execution_stats": stats, "recent_failures": failures}


@router.post("/n8n/callback")
async def n8n_callback(request: Request):
    body = await request.body()
    try:
        return service.handle_callback(body, request.headers.get("x-pulse-timestamp"), request.headers.get("x-pulse-signature"))
    except guard.GuardError as exc:
        raise _guard_http(exc)


@router.post("/n8n/error")
async def n8n_error(request: Request):
    """n8n Error Trigger workflow → mark the matching execution failed (signed)."""
    body = await request.body()
    try:
        guard.verify_webhook(body, request.headers.get("x-pulse-timestamp"), request.headers.get("x-pulse-signature"), source="service:n8n")
        msg = json.loads(body)
    except guard.GuardError as exc:
        raise _guard_http(exc)
    except ValueError:
        raise HTTPException(422, "Malformed error report")
    n8n_id = str(msg.get("failed_n8n_execution_id") or "")[:40]
    message = str(msg.get("message") or "n8n workflow error")[:400]
    with connect() as conn:
        row = conn.execute("SELECT e.id, e.action_id, a.run_id FROM executions e JOIN actions a ON a.id=e.action_id"
                           " WHERE e.n8n_execution_id=? AND e.status IN ('submitted','dispatching','authorized')", (n8n_id,)).fetchone()
        if row:
            now = audit.now_iso()
            conn.execute("UPDATE executions SET status='failed', error=?, completed_at=?, updated_at=? WHERE id=?",
                         (f"n8n error in {msg.get('node')}: {message}", now, now, row["id"]))
            conn.execute("UPDATE actions SET status='execution_failed', updated_at=? WHERE id=?", (now, row["action_id"]))
        audit.append("workflow.error", "service:n8n", f"n8n workflow error: {message[:150]}",
                     run_id=row["run_id"] if row else None, action_id=row["action_id"] if row else None,
                     execution_id=row["id"] if row else None,
                     detail={"n8n_execution_id": n8n_id, "workflow": msg.get("workflow"), "node": msg.get("node")}, conn=conn)
    if row:
        service.refresh_run_status(row["run_id"])
    return {"accepted": True, "matched_execution": row["id"] if row else None}


@router.post("/workflows/scan")
async def trigger_scan(request: Request, actor: Actor = Depends(current_actor)):
    """Manager-triggered risk scan that runs through n8n's scan workflow (round trip)."""
    import httpx

    require_csrf(request)
    require(actor, "action:approve:standard")
    settings = get_settings()
    from .actions import canonical

    body = canonical({"requested_by": actor.id}).encode()  # n8n verifies over canonical JSON
    try:
        ts, sig = guard.sign(body)
    except RuntimeError:
        raise HTTPException(503, "PULSE_WEBHOOK_SECRET is not configured")
    url = settings.n8n_base_url.rstrip("/") + "/webhook/pulse-scan"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, content=body, headers={"Content-Type": "application/json",
                                                               "X-Pulse-Timestamp": ts, "X-Pulse-Signature": sig})
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"n8n unreachable ({type(exc).__name__})")
    if r.status_code >= 300:
        raise HTTPException(502, f"n8n scan workflow returned HTTP {r.status_code}")
    audit.append("signal.scan_requested", actor.id, f"{actor.name} triggered the n8n risk scan")
    try:
        return {"n8n_status": r.status_code, "result": r.json()}
    except ValueError:
        return {"n8n_status": r.status_code, "result": None}


@router.post("/n8n/scan")
async def n8n_scan(request: Request):
    """Risk-triggered workflow: n8n asks Pulse to investigate new critical signals (signed)."""
    body = await request.body()
    try:
        guard.verify_webhook(body, request.headers.get("x-pulse-timestamp"), request.headers.get("x-pulse-signature"), source="service:n8n")
    except guard.GuardError as exc:
        raise _guard_http(exc)
    try:
        options = json.loads(body or b"{}")
    except ValueError:
        options = {}
    max_runs = max(0, min(int(options.get("max_runs", 1)), 3))
    sid = get_settings().snapshot_id
    started, skipped = [], []
    today = audit.now_iso()[:10]
    system_actor = Actor(id="service:n8n", name="n8n risk scan", role="service", kind="service",
                         permissions=frozenset({"investigation:start"}))
    for sig in signals.scan(sid, 50):
        if sig["severity"] != "critical" or len(started) >= max_runs:
            continue
        with connect() as conn:
            claimed = conn.execute("INSERT OR IGNORE INTO signal_triggers VALUES (?,?,?,?,?)",
                                   (sig["customer_id"], sig["key"], today, None, audit.now_iso())).rowcount
        if not claimed:
            skipped.append(sig["customer_id"])
            continue
        run_id = await service.runtime().start(system_actor, sig["customer_id"],
                                               f"Risk-triggered investigation: {sig['title']}", "standard")
        with connect() as conn:
            conn.execute("UPDATE signal_triggers SET run_id=? WHERE customer_id=? AND signal_key=? AND day=?",
                         (run_id, sig["customer_id"], sig["key"], today))
        started.append({"customer_id": sig["customer_id"], "run_id": run_id, "signal": sig["key"]})
    audit.append("signal.scan", "service:n8n", f"Risk scan started {len(started)} investigation(s)",
                 detail={"started": started, "already_triggered_today": skipped})
    return {"started": started, "already_triggered_today": skipped}


# ---------------------------------------------------------------- audit & security

@router.get("/audit")
def audit_trail(actor: Actor = Depends(current_actor), run_id: str | None = None, action_id: str | None = None,
                kind: str | None = None, limit: int = Query(100, ge=1, le=500), before_id: int | None = None):
    require(actor, "audit:read")
    return {"items": audit.list_events(run_id=run_id, action_id=action_id, kind=kind, limit=limit, before_id=before_id),
            "chain": audit.verify_chain(),
            "integrity_note": "Hash-chained and append-only via SQLite triggers. Tamper-evident, not tamper-proof: anyone with file access could rewrite the file."}


@router.get("/security/decisions")
def decisions(actor: Actor = Depends(current_actor), limit: int = Query(100, ge=1, le=500)):
    require(actor, "security:read")
    with connect() as conn:
        rows = conn.execute("SELECT * FROM policy_decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"items": [_row(r) for r in rows]}


@router.get("/security/events")
def security_events(actor: Actor = Depends(current_actor), limit: int = Query(100, ge=1, le=500)):
    require(actor, "security:read")
    with connect() as conn:
        rows = conn.execute("SELECT * FROM security_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"items": [_row(r) for r in rows]}


@router.get("/security/policy")
def policy(actor: Actor = Depends(current_actor)):
    require(actor, "pulse:read")
    return {
        "actions": [{"action_type": s.action_type, "label": s.label, "risk": s.risk, "required_role": s.required_role,
                     "approval": "auto (low risk)" if s.approval_permission is None and get_settings().auto_approve_low_risk
                     else f"human: {s.required_role}+", "external": s.external, "destination": s.destination}
                    for s in REGISTRY.values()],
        "default": "deny (any action type not listed)",
        "proposers": sorted(guard.PROPOSER_AGENTS),
        "limits": {"max_discount_pct": guard.MAX_DISCOUNT_PCT, "max_discount_months": guard.MAX_DISCOUNT_MONTHS,
                   "approval_ttl_seconds": get_settings().approval_ttl_seconds},
        "roles": {role: sorted(perms) for role, perms in auth.PERMISSIONS.items()},
    }


@router.post("/security/probes/{probe}")
async def run_probe(probe: Literal["replay", "tamper", "unauthorized", "forged_webhook", "duplicate_callback", "unknown_action"],
                    request: Request, actor: Actor = Depends(current_actor)):
    """Executes a real attack attempt against the live enforcement code and reports the outcome."""
    require_csrf(request)
    require(actor, "security:probe")
    from .probes import run

    return await run(probe, actor)


@router.get("/customers/{customer_id}/context")
def customer_context(customer_id: str, actor: Actor = Depends(current_actor)):
    require(actor, "pulse:read")
    sid = get_settings().snapshot_id
    if not tools.customer_exists(sid, customer_id):
        raise HTTPException(404, "Unknown customer")
    return {"profile": tools.get_profile(sid, customer_id), "activity": signals.for_customer(sid, customer_id),
            "due_requirements": tools.get_due_requirements(sid, customer_id),
            "crm": tools.get_crm_context(sid, customer_id), "peers": tools.get_peer_benchmark(sid, customer_id)}
