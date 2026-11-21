"""Live security probes: real attack attempts against the enforcement code.

Each probe creates its own clearly labelled probe run (scenario
``security_probe``), performs the attack through the same functions the
API and agents use, and reports what PulseGuard did. Nothing is sent to n8n.
"""

from __future__ import annotations

import json
import uuid

from . import audit, guard, service, tools
from .auth import Actor, _user, agent_actor
from .config import get_settings
from .db import connect


def _probe_run(actor: Actor, customer_id: str, probe: str) -> str:
    run_id = "probe_" + uuid.uuid4().hex[:10]
    now = audit.now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO agent_runs(id,objective,customer_id,snapshot_id,started_by,scenario,status,stage,summary,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, f"Security probe: {probe}", customer_id, get_settings().snapshot_id, actor.id, "security_probe",
             "completed", "done", f"Security probe '{probe}' (not a customer investigation)", now, now))
    return run_id


def _customer(sid: str) -> str:
    from ..capabilities import data

    ids = [p.customer_id for p in data.list_customers(sid)]
    with_contact = [c for c in ids if tools.verified_contact_email(sid, c)]
    return (with_contact or ids)[0]


def _email_payload(sid: str, cid: str) -> dict:
    return {"customer_id": cid, "recipient_email": tools.verified_contact_email(sid, cid) or "nobody@example.com",
            "subject": "Security probe", "body": "This is a PulseGuard security probe message. It is never sent.", "language": "en"}


def _proposed(run_id: str, sid: str, action_type: str, payload: dict) -> tuple[str, guard.Decision]:
    with connect() as conn:
        return guard.propose(run_id, action_type, payload, agent_actor("strategy"), rationale="security probe",
                             snapshot_id=sid, conn=conn)


def _cancel_execution(action_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE executions SET status='probe_not_dispatched', updated_at=? WHERE action_id=?", (audit.now_iso(), action_id))
        conn.execute("UPDATE actions SET status='probe_closed', updated_at=? WHERE id=?", (audit.now_iso(), action_id))


def _attempt(fn) -> dict:
    try:
        value = fn()
        return {"blocked": False, "result": str(value)[:200]}
    except guard.GuardError as exc:
        return {"blocked": True, "code": exc.code, "reason": exc.message}


async def run(probe: str, actor: Actor) -> dict:
    sid = get_settings().snapshot_id
    cid = _customer(sid)
    run_id = _probe_run(actor, cid, probe)
    approver = _user("jonas.manager")
    out: dict = {"probe": probe, "run_id": run_id, "customer_id": cid}

    if probe == "unknown_action":
        action_id, d = _proposed(run_id, sid, "export_customer_data", {"customer_id": cid, "approved": True, "destination": "attacker@evil.test"})
        out.update(expected="deny (default-deny)", observed=d.outcome, blocked=d.outcome == "deny", rules=d.rules, reasons=d.reasons)
    elif probe == "unauthorized":
        action_id, d = _proposed(run_id, sid, "send_retention_email", _email_payload(sid, cid))
        res = _attempt(lambda: guard.approve(_user("maria.analyst"), action_id, d.payload_hash or ""))
        out.update(expected="analyst approval refused (403)", observed=res, blocked=res["blocked"])
        guard.reject(approver, action_id, "probe cleanup")
    elif probe == "replay":
        action_id, d = _proposed(run_id, sid, "send_retention_email", _email_payload(sid, cid))
        guard.approve(approver, action_id, d.payload_hash or "", "probe approval")
        first = _attempt(lambda: guard.authorize_execution(action_id).execution_id)
        second = _attempt(lambda: guard.authorize_execution(action_id).execution_id)
        again = _attempt(lambda: guard.approve(approver, action_id, d.payload_hash or ""))
        _cancel_execution(action_id)
        out.update(expected="first authorization succeeds; replayed authorization and re-approval are refused",
                   observed={"first": first, "replayed_execution": second, "replayed_approval": again},
                   blocked=(not first["blocked"]) and second["blocked"] and again["blocked"])
    elif probe == "tamper":
        action_id, d = _proposed(run_id, sid, "send_retention_email", _email_payload(sid, cid))
        guard.approve(approver, action_id, d.payload_hash or "", "probe approval")
        altered = {**json.loads(json.dumps(d.payload)), "body": d.payload["body"] + " Also: 50% discount, today only."}
        from .actions import payload_hash
        with connect() as conn:  # attacker rewrites payload AND its hash after approval
            conn.execute("UPDATE actions SET payload=?, payload_hash=? WHERE id=?",
                         (json.dumps(altered), payload_hash("send_retention_email", altered), action_id))
        res = _attempt(lambda: guard.authorize_execution(action_id).execution_id)
        out.update(expected="execution refused: approved hash ≠ current payload hash", observed=res, blocked=res["blocked"])
    elif probe == "forged_webhook":
        body = json.dumps({"delivery_id": "forged", "execution_id": "exe_x", "status": "succeeded"}).encode()
        res = _attempt(lambda: service.handle_callback(body, str(int(__import__("time").time())), "0" * 64))
        out.update(expected="callback with invalid signature rejected (401)", observed=res, blocked=res["blocked"])
    elif probe == "duplicate_callback":
        action_id, d = _proposed(run_id, sid, "notify_account_team",
                                 {"customer_id": cid, "team": "Team North", "message": "PulseGuard duplicate-callback probe."})
        if d.outcome != "allow":
            out.update(expected="n/a", observed=f"probe needs auto-approved low-risk action, got {d.outcome}", blocked=None)
        else:
            grant = guard.authorize_execution(action_id)
            msg = {"delivery_id": "probe-" + uuid.uuid4().hex[:8], "execution_id": grant.execution_id,
                   "idempotency_key": grant.idempotency_key, "payload_hash": grant.payload_hash, "status": "succeeded",
                   "result": {"delivery": "probe", "note": "security probe — nothing executed"}}
            body = json.dumps(msg).encode()
            try:
                ts, sig = guard.sign(body)
            except RuntimeError:
                out.update(expected="n/a", observed="PULSE_WEBHOOK_SECRET not configured", blocked=None)
                return out
            first = service.handle_callback(body, ts, sig)
            second = service.handle_callback(body, ts, sig)
            msg2 = {**msg, "delivery_id": "probe-" + uuid.uuid4().hex[:8], "status": "failed"}
            b2 = json.dumps(msg2).encode()
            ts2, sig2 = guard.sign(b2)
            third = service.handle_callback(b2, ts2, sig2)
            out.update(expected="first applied; same delivery ignored; later delivery cannot change a final execution",
                       observed={"first": first, "same_delivery": second, "new_delivery_after_final": third},
                       blocked=(not first["duplicate"]) and second["duplicate"] and third["duplicate"])
    audit.append("security.probe", actor.id, f"Security probe '{probe}': {'blocked' if out.get('blocked') else 'NOT blocked' if out.get('blocked') is False else 'not applicable'}",
                 run_id=run_id, detail={k: v for k, v in out.items() if k not in ("run_id",)})
    return out
