"""Workflow Execution agent's transport to n8n.

``dispatch`` only accepts an ``ExecutionGrant`` minted by PulseGuard and
re-verifies its HMAC, so no agent or route can reach n8n without passing
``guard.authorize_execution`` first. Requests are signed with the shared
webhook secret (HMAC-SHA256 over ``timestamp.body``).
"""

from __future__ import annotations

import json
import uuid

import httpx

from . import audit, guard
from .actions import canonical
from .config import get_settings
from .db import connect

UNREACHABLE_URL = "http://127.0.0.1:9"  # discard port: used by the n8n-outage scenario
_transport: httpx.AsyncBaseTransport | None = None  # tests inject an emulated n8n here


async def health(base_url: str | None = None) -> dict:
    url = (base_url or get_settings().n8n_base_url).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=3, transport=_transport) as client:
            r = await client.get(f"{url}/healthz")
        return {"reachable": r.status_code == 200, "status_code": r.status_code, "url": url}
    except httpx.HTTPError as exc:
        return {"reachable": False, "error": type(exc).__name__, "url": url}


def _mark(execution_id: str, **fields) -> None:
    sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
    with connect() as conn:
        conn.execute(f"UPDATE executions SET {sets} WHERE id=?", (*fields.values(), audit.now_iso(), execution_id))


async def dispatch(grant: guard.ExecutionGrant, *, run_id: str, base_url: str | None = None) -> dict:
    if not guard.verify_grant(grant):
        guard.security_event("forged_grant", "critical", "unknown", grant.action_id, {"execution_id": grant.execution_id})
        raise guard.GuardError("forged_grant", "Execution grant failed verification; dispatch refused.", 403)
    settings = get_settings()
    url = (base_url or settings.n8n_base_url).rstrip("/") + settings.n8n_execute_path
    envelope = {
        "execution_id": grant.execution_id,
        "action_id": grant.action_id,
        "action_type": grant.action_type,
        "destination": grant.destination,
        "payload": grant.payload,
        "payload_hash": grant.payload_hash,
        "idempotency_key": grant.idempotency_key,
        "callback_url": settings.pulse_callback_base_url.rstrip("/") + "/api/pulse/n8n/callback",
        "request_id": uuid.uuid4().hex,
    }
    body = canonical(envelope).encode()
    with connect() as conn:
        conn.execute("UPDATE executions SET attempts=attempts+1, status='dispatching', updated_at=? WHERE id=?",
                     (audit.now_iso(), grant.execution_id))
    try:
        ts, sig = guard.sign(body)
    except RuntimeError as exc:
        return _failed(grant, run_id, "not_configured", str(exc))
    try:
        async with httpx.AsyncClient(timeout=settings.n8n_timeout_seconds, transport=_transport) as client:
            r = await client.post(url, content=body, headers={
                "Content-Type": "application/json",
                "X-Pulse-Timestamp": ts,
                "X-Pulse-Signature": sig,
                "X-Idempotency-Key": grant.idempotency_key,
            })
    except httpx.TimeoutException:
        return _failed(grant, run_id, "timeout", f"n8n did not answer within {settings.n8n_timeout_seconds:.0f}s")
    except httpx.HTTPError as exc:
        return _failed(grant, run_id, "unreachable", f"n8n unreachable at {url.split('/webhook')[0]} ({type(exc).__name__})")
    if r.status_code >= 300:
        detail = r.text[:200].replace(settings.pulse_webhook_secret.get_secret_value() or "\x00", "***")
        return _failed(grant, run_id, "rejected", f"n8n responded HTTP {r.status_code}: {detail}")
    try:
        reply = r.json()
    except json.JSONDecodeError:
        reply = {}
    n8n_id = str(reply.get("n8n_execution_id") or reply.get("executionId") or "") or None
    with connect() as conn:
        # A fast callback may already have completed the execution; never regress it.
        conn.execute(
            "UPDATE executions SET status=CASE WHEN status IN ('succeeded','failed') THEN status ELSE 'submitted' END,"
            " n8n_execution_id=COALESCE(n8n_execution_id, ?), submitted_at=?, error=NULL, updated_at=? WHERE id=?",
            (n8n_id, audit.now_iso(), audit.now_iso(), grant.execution_id))
        audit.append("execution.submitted", "agent:workflow_execution", f"Submitted {grant.action_type} to n8n",
                     run_id=run_id, action_id=grant.action_id, execution_id=grant.execution_id,
                     detail={"n8n_execution_id": n8n_id, "destination": grant.destination}, conn=conn)
    return {"status": "submitted", "n8n_execution_id": n8n_id}


def _failed(grant: guard.ExecutionGrant, run_id: str, category: str, message: str) -> dict:
    with connect() as conn:
        conn.execute("UPDATE executions SET status='dispatch_failed', error=?, updated_at=? WHERE id=?",
                     (message, audit.now_iso(), grant.execution_id))
        audit.append("execution.dispatch_failed", "agent:workflow_execution", message, run_id=run_id,
                     action_id=grant.action_id, execution_id=grant.execution_id, detail={"category": category}, conn=conn)
    return {"status": "dispatch_failed", "error": message, "category": category}
