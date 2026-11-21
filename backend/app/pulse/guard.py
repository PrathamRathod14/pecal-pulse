"""PulseGuard — deterministic, server-side policy enforcement.

PulseGuard never consults an LLM. It decides from: the action registry, the
strict payload schema, the requesting actor's server-side identity, business
rules, the stored approval record and the payload hash. Every decision is
persisted to ``policy_decisions`` and every denial to ``security_events``.

Lifecycle enforced here:
  propose  -> evaluate()            allow | require_approval | deny
  decide   -> approve() / reject()  binds approval to the exact payload hash
  execute  -> authorize_execution() re-checks everything, consumes the
                                    approval exactly once, returns a signed grant
  callback -> verify_webhook()      HMAC + timestamp skew + delivery dedupe
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import audit, tools
from .actions import REGISTRY, normalize, payload_hash
from .auth import ROLE_RANK, Actor
from .config import get_settings
from .db import connect

PROPOSER_AGENTS = {"agent:strategy"}  # least privilege: only Strategy may propose
INJECTION_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"ignore (all |any )?(previous|prior|above) instructions",
        r"\badmin mode\b",
        r"\bsystem prompt\b",
        r"approved\s*[=:]\s*true",
        r"\bexport (the )?(full |entire )?customer (list|data|database)\b",
        r"you are now",
    )
]
URL_RE = re.compile(r"https?://|www\.", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MAX_DISCOUNT_PCT = 15.0
MAX_DISCOUNT_MONTHS = 6


class GuardError(Exception):
    """A PulseGuard refusal. ``code`` is stable for clients and tests."""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass
class Decision:
    outcome: str  # allow | require_approval | deny
    action_type: str
    risk: str | None = None
    required_role: str | None = None
    approval_permission: str | None = None
    rules: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    payload: dict | None = None
    payload_hash: str | None = None
    annotations: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "payload"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


# ---------------------------------------------------------------------------
# 1. Proposal evaluation
# ---------------------------------------------------------------------------

def evaluate(action_type: str, raw_payload, requester: Actor, *, snapshot_id: str,
             today: date | None = None) -> Decision:
    today = today or _now().date()
    settings = get_settings()
    deny = lambda rule, reason, **kw: Decision("deny", action_type, rules=[rule], reasons=[reason], **kw)

    spec = REGISTRY.get(action_type)
    if spec is None:
        return deny("PG-001 default-deny", f"Action type '{action_type}' is not in the PulseGuard registry; unknown actions are denied by default.")

    if requester.kind == "agent" and requester.id not in PROPOSER_AGENTS:
        return deny("PG-002 proposer-scope", f"{requester.id} is not permitted to propose actions (only the Strategy agent may).", risk=spec.risk)
    if requester.kind == "human" and not requester.can("investigation:start"):
        return deny("PG-002 proposer-scope", f"Role '{requester.role}' may not propose actions.", risk=spec.risk)

    payload, errors = normalize(action_type, raw_payload)
    if payload is None:
        return deny("PG-003 schema", "Payload failed strict validation: " + "; ".join(errors[:5]), risk=spec.risk)

    rules = ["PG-001 registry", "PG-002 proposer-scope", "PG-003 schema"]
    reasons: list[str] = []
    annotations: dict = {}

    texts = _strings(payload)
    if any(p.search(t) for p in INJECTION_PATTERNS for t in texts):
        return deny("PG-004 injection", "Payload contains instruction-like text typical of prompt injection; it is treated as data and the action is blocked.", risk=spec.risk)
    rules.append("PG-004 injection-scan")

    customer_id = payload["customer_id"]
    if not tools.customer_exists(snapshot_id, customer_id):
        return deny("PG-005 customer-scope", f"Customer {customer_id} is not in the active snapshot.", risk=spec.risk)
    rules.append("PG-005 customer-scope")

    # ---- per-type business rules
    if action_type == "create_followup_task":
        if not (today + timedelta(days=1) <= date.fromisoformat(payload["due_date"]) <= today + timedelta(days=45)):
            return deny("PG-101 followup-window", "Follow-up due date must be 1–45 days from today.", risk=spec.risk)
        if payload["owner"] not in tools.known_teams(snapshot_id):
            return deny("PG-102 owner", f"Owner '{payload['owner']}' is not a known team.", risk=spec.risk)
        rules += ["PG-101 followup-window", "PG-102 owner"]
    elif action_type == "notify_account_team":
        if payload["team"] not in tools.known_teams(snapshot_id):
            return deny("PG-102 owner", f"Team '{payload['team']}' is not a known team.", risk=spec.risk)
        if URL_RE.search(payload["message"]) or EMAIL_RE.search(payload["message"]):
            return deny("PG-103 no-links", "Internal notifications may not contain links or e-mail addresses.", risk=spec.risk)
        rules += ["PG-102 owner", "PG-103 no-links"]
    elif action_type == "schedule_account_review":
        if not (today + timedelta(days=1) <= date.fromisoformat(payload["review_date"]) <= today + timedelta(days=60)):
            return deny("PG-104 review-window", "Review date must be 1–60 days from today.", risk=spec.risk)
        rules.append("PG-104 review-window")
    elif action_type == "send_retention_email":
        verified = tools.verified_contact_email(snapshot_id, customer_id)
        if not verified:
            return deny("PG-201 verified-recipient", "No verified contact is on file for this account; PulseGuard will not send to an address supplied by an agent.", risk=spec.risk)
        if payload["recipient_email"].lower() != verified.lower():
            return deny("PG-201 verified-recipient", "Recipient differs from the verified contact on file (possible exfiltration attempt).", risk=spec.risk)
        if URL_RE.search(payload["body"]) or URL_RE.search(payload["subject"]):
            return deny("PG-202 no-links", "Customer e-mails generated by agents may not contain links.", risk=spec.risk)
        other_addresses = {e.lower() for e in EMAIL_RE.findall(payload["body"])} - {verified.lower()}
        if other_addresses:
            return deny("PG-203 no-foreign-addresses", "E-mail body references other e-mail addresses.", risk=spec.risk)
        rules += ["PG-201 verified-recipient", "PG-202 no-links", "PG-203 no-foreign-addresses"]
        annotations["delivery_mode"] = "external" if settings.allow_external_email else "simulated"
        if not settings.allow_external_email:
            reasons.append("External delivery is disabled (PULSE_ALLOW_EXTERNAL_EMAIL=false): n8n will render a preview only.")
    elif action_type == "offer_retention_discount":
        if payload["discount_pct"] > MAX_DISCOUNT_PCT:
            return deny("PG-301 discount-cap", f"Discounts above {MAX_DISCOUNT_PCT:.0f}% are never authorized by PulseGuard.", risk=spec.risk)
        if payload["duration_months"] > MAX_DISCOUNT_MONTHS:
            return deny("PG-302 discount-duration", f"Discounts may last at most {MAX_DISCOUNT_MONTHS} months.", risk=spec.risk)
        rules += ["PG-301 discount-cap", "PG-302 discount-duration", "PG-303 separation-of-duties"]
        annotations["separation_of_duties"] = True

    digest = payload_hash(action_type, payload)
    common = dict(risk=spec.risk, required_role=spec.required_role, approval_permission=spec.approval_permission,
                  payload=payload, payload_hash=digest, annotations=annotations)
    if spec.approval_permission is None and settings.auto_approve_low_risk:
        reasons.insert(0, "Low-risk internal action: auto-approved by policy (PULSE_AUTO_APPROVE_LOW_RISK).")
        return Decision("allow", action_type, rules=rules + ["PG-010 low-risk-auto"], reasons=reasons, **common)
    role = spec.required_role if spec.approval_permission else "sales_manager"
    if spec.approval_permission is None:
        common["approval_permission"] = "action:approve:standard"
    common["required_role"] = role
    reasons.insert(0, f"{spec.risk.title()}-risk action: requires explicit approval by role '{role}' or higher.")
    return Decision("require_approval", action_type, rules=rules + ["PG-011 human-approval"], reasons=reasons, **common)


def record_decision(decision: Decision, *, phase: str, actor: str, action_id: str | None,
                    run_id: str | None, conn) -> None:
    conn.execute(
        "INSERT INTO policy_decisions(action_id,run_id,phase,actor,action_type,outcome,risk,rules,reasons,payload_hash,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (action_id, run_id, phase, actor, decision.action_type, decision.outcome, decision.risk,
         json.dumps(decision.rules), json.dumps(decision.reasons), decision.payload_hash, audit.now_iso()),
    )
    if decision.outcome == "deny":
        security_event("policy_denied", "warning", actor, action_id or decision.action_type,
                       {"phase": phase, "rules": decision.rules, "reasons": decision.reasons}, conn=conn)


def security_event(kind: str, severity: str, actor: str | None, subject: str | None, detail: dict, *, conn=None) -> None:
    if conn is None:
        with connect() as own:
            return security_event(kind, severity, actor, subject, detail, conn=own)
    conn.execute(
        "INSERT INTO security_events(kind,severity,actor,subject,detail,created_at) VALUES (?,?,?,?,?,?)",
        (kind, severity, actor, subject, json.dumps(detail, ensure_ascii=False), audit.now_iso()),
    )


def propose(run_id: str, action_type: str, payload, requester: Actor, *, rationale: str,
            snapshot_id: str, ttl_seconds: int | None = None, conn) -> tuple[str, Decision]:
    """Evaluate + persist a proposed action inside the caller's transaction."""
    decision = evaluate(action_type, payload, requester, snapshot_id=snapshot_id)
    action_id = "act_" + uuid.uuid4().hex[:16]
    now = _now()
    stored_payload = decision.payload if decision.payload is not None else payload
    digest = decision.payload_hash or payload_hash(action_type, stored_payload if isinstance(stored_payload, dict) else {"raw": str(stored_payload)})
    status = {"allow": "auto_approved", "require_approval": "pending_approval", "deny": "blocked"}[decision.outcome]
    ttl = ttl_seconds or get_settings().approval_ttl_seconds
    expires = _iso(now + timedelta(seconds=ttl)) if decision.outcome == "require_approval" else None
    customer = stored_payload.get("customer_id", "unknown") if isinstance(stored_payload, dict) else "unknown"
    conn.execute(
        "INSERT INTO actions(id,run_id,customer_id,action_type,payload,payload_hash,requested_by,rationale,status,risk,required_role,policy_outcome,expires_at,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (action_id, run_id, str(customer)[:40], action_type, json.dumps(stored_payload, ensure_ascii=False, default=str), digest,
         requester.id, rationale[:1000], status, decision.risk, decision.required_role, json.dumps(decision.public()),
         expires, _iso(now), _iso(now)),
    )
    record_decision(decision, phase="proposal", actor=requester.id, action_id=action_id, run_id=run_id, conn=conn)
    audit.append(f"action.{status}", "system:pulseguard",
                 f"{action_type} → {decision.outcome.replace('_', ' ')}",
                 run_id=run_id, action_id=action_id,
                 detail={"rules": decision.rules, "reasons": decision.reasons, "payload_hash": digest}, conn=conn)
    return action_id, decision


# ---------------------------------------------------------------------------
# 2. Human decisions
# ---------------------------------------------------------------------------

def _load_action(conn, action_id: str):
    row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    if row is None:
        raise GuardError("not_found", "Unknown action", 404)
    return row


def expire_due(conn=None) -> list[str]:
    """Mark pending actions past their expiry as expired. Returns run ids touched."""
    if conn is None:
        with connect() as own:
            return expire_due(own)
    now = _iso(_now())
    rows = conn.execute(
        "SELECT id, run_id FROM actions WHERE status='pending_approval' AND expires_at IS NOT NULL AND expires_at<=?", (now,)
    ).fetchall()
    for row in rows:
        conn.execute("UPDATE actions SET status='expired', updated_at=? WHERE id=? AND status='pending_approval'", (now, row["id"]))
        audit.append("action.expired", "system:pulseguard", "Approval request expired without a decision",
                     run_id=row["run_id"], action_id=row["id"], conn=conn)
    return sorted({r["run_id"] for r in rows})


def _flush_refusal(fx: list[tuple], exc: "GuardError", actor: Actor, action_id: str) -> None:
    """Persist refusal side effects after the decision transaction rolled back."""
    if not fx:
        return
    with connect() as conn:
        for item in fx:
            if item[0] == "security":
                _, kind, severity, detail = item
                security_event(kind, severity, actor.id, action_id, {**detail, "code": exc.code}, conn=conn)
            elif item[0] == "status":
                conn.execute("UPDATE actions SET status=?, updated_at=? WHERE id=?", (item[1], _iso(_now()), action_id))
            elif item[0] == "expire":
                expire_due(conn)


def _check_decidable(conn, actor: Actor, action, presented_hash: str | None, deciding: str, fx: list):
    if action["status"] == "pending_approval" and action["expires_at"] and action["expires_at"] <= _iso(_now()):
        fx.append(("expire",))
        fx.append(("security", "decision_after_expiry", "warning", {"attempted": deciding}))
        raise GuardError("expired", "This approval request has expired; the agents must propose the action again.", 410)
    if action["status"] != "pending_approval":
        fx.append(("security", "decision_replay", "warning", {"status": action["status"], "attempted": deciding}))
        raise GuardError("not_pending", f"Action is '{action['status']}', not pending approval — duplicate or replayed decision rejected.")
    spec = REGISTRY.get(action["action_type"])
    decision = json.loads(action["policy_outcome"])
    permission = decision.get("approval_permission") or (spec.approval_permission if spec else None)
    if not permission or not actor.can(permission) or ROLE_RANK.get(actor.role, -1) < ROLE_RANK.get(action["required_role"] or "sales_manager", 99):
        fx.append(("security", "unauthorized_decision", "high", {"role": actor.role, "needed": permission, "attempted": deciding}))
        raise GuardError("forbidden", f"Role '{actor.role}' cannot {deciding} this action (needs '{permission}' / role '{action['required_role']}').", 403)
    if deciding == "approve":
        stored = json.loads(action["payload"])
        if payload_hash(action["action_type"], stored) != action["payload_hash"]:
            fx.append(("security", "payload_integrity", "critical", {"reason": "stored payload hash mismatch"}))
            fx.append(("status", "blocked"))
            raise GuardError("integrity", "Stored payload no longer matches its hash; approval refused.", 409)
        if presented_hash != action["payload_hash"]:
            fx.append(("security", "payload_mismatch", "high", {"presented": (presented_hash or "")[:64]}))
            raise GuardError("payload_mismatch", "The payload you reviewed differs from the stored action. Reload and review again.", 409)
        if decision.get("annotations", {}).get("separation_of_duties"):
            starter = conn.execute("SELECT started_by FROM agent_runs WHERE id=?", (action["run_id"],)).fetchone()
            if starter and starter["started_by"] == actor.id:
                fx.append(("security", "separation_of_duties", "warning", {}))
                raise GuardError("separation_of_duties", "Financial actions must be approved by someone other than the person who started the investigation.", 403)
    return permission


def approve(actor: Actor, action_id: str, presented_hash: str, comment: str = "") -> dict:
    fx: list[tuple] = []
    try:
        return _approve(actor, action_id, presented_hash, comment, fx)
    except GuardError as exc:
        _flush_refusal(fx, exc, actor, action_id)
        raise


def _approve(actor: Actor, action_id: str, presented_hash: str, comment: str, fx: list) -> dict:
    with connect() as conn:
        action = _load_action(conn, action_id)
        _check_decidable(conn, actor, action, presented_hash, "approve", fx)
        run = conn.execute("SELECT snapshot_id FROM agent_runs WHERE id=?", (action["run_id"],)).fetchone()
        # Policy may have changed since proposal: re-evaluate the stored payload.
        recheck = evaluate(action["action_type"], json.loads(action["payload"]), Actor(
            id=action["requested_by"], name="", role="agent", kind="agent"), snapshot_id=run["snapshot_id"])
        if recheck.outcome == "deny":
            fx.append(("security", "policy_denied", "warning", {"phase": "approval", "rules": recheck.rules, "reasons": recheck.reasons}))
            fx.append(("status", "blocked"))
            raise GuardError("policy_denied", "PulseGuard now denies this action: " + "; ".join(recheck.reasons), 409)
        record_decision(recheck, phase="approval", actor=actor.id, action_id=action_id, run_id=action["run_id"], conn=conn)
        now = _now()
        approval_id = "apr_" + uuid.uuid4().hex[:16]
        conn.execute(
            "INSERT INTO approvals(id,action_id,decision,approver,approver_role,payload_hash,comment,decided_at,expires_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (approval_id, action_id, "approved", actor.id, actor.role, action["payload_hash"], comment[:500],
             _iso(now), _iso(now + timedelta(seconds=get_settings().approval_ttl_seconds))),
        )
        conn.execute("UPDATE actions SET status='approved', updated_at=? WHERE id=? AND status='pending_approval'", (_iso(now), action_id))
        audit.append("approval.granted", actor.id, f"{actor.name} approved {action['action_type']}",
                     run_id=action["run_id"], action_id=action_id,
                     detail={"approval_id": approval_id, "payload_hash": action["payload_hash"], "comment": comment[:500]}, conn=conn)
    return {"approval_id": approval_id, "action_id": action_id, "status": "approved", "run_id": action["run_id"]}


def reject(actor: Actor, action_id: str, comment: str = "") -> dict:
    fx: list[tuple] = []
    try:
        with connect() as conn:
            action = _load_action(conn, action_id)
            _check_decidable(conn, actor, action, None, "reject", fx)
            now = _now()
            approval_id = "apr_" + uuid.uuid4().hex[:16]
            conn.execute(
                "INSERT INTO approvals(id,action_id,decision,approver,approver_role,payload_hash,comment,decided_at,expires_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (approval_id, action_id, "rejected", actor.id, actor.role, action["payload_hash"], comment[:500], _iso(now), _iso(now)),
            )
            conn.execute("UPDATE actions SET status='rejected', updated_at=? WHERE id=?", (_iso(now), action_id))
            audit.append("approval.rejected", actor.id, f"{actor.name} rejected {action['action_type']}",
                         run_id=action["run_id"], action_id=action_id, detail={"comment": comment[:500]}, conn=conn)
    except GuardError as exc:
        _flush_refusal(fx, exc, actor, action_id)
        raise
    return {"approval_id": approval_id, "action_id": action_id, "status": "rejected", "run_id": action["run_id"]}


# ---------------------------------------------------------------------------
# 3. Execution authorization (immediately before dispatch)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutionGrant:
    execution_id: str
    action_id: str
    action_type: str
    payload: dict
    payload_hash: str
    idempotency_key: str
    destination: str
    issued_at: int
    signature: str

    def message(self) -> bytes:
        return f"{self.execution_id}|{self.action_id}|{self.payload_hash}|{self.idempotency_key}|{self.issued_at}".encode()


def _grant_key() -> bytes:
    return hashlib.sha256(b"grant:" + get_settings().session_secret()).digest()


def verify_grant(grant: ExecutionGrant, max_age: int = 120) -> bool:
    expected = hmac.new(_grant_key(), grant.message(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, grant.signature) and (time.time() - grant.issued_at) <= max_age


def authorize_execution(action_id: str, *, retry_execution_id: str | None = None) -> ExecutionGrant:
    """The single gate in front of n8n. Consumes the approval exactly once.

    Refusals are recorded (security event, audit, status change) in their own
    transaction after the authorization transaction rolls back.
    """
    failure: dict = {}
    try:
        return _authorize(action_id, retry_execution_id, failure)
    except GuardError as exc:
        if failure:
            with connect() as conn:
                security_event("execution_blocked", failure.get("severity", "high"), "system:pulseguard", action_id,
                               {"code": exc.code, "reason": exc.message}, conn=conn)
                run = conn.execute("SELECT run_id FROM actions WHERE id=?", (action_id,)).fetchone()
                if failure.get("status") and run:
                    conn.execute("UPDATE actions SET status=?, updated_at=? WHERE id=?",
                                 (failure["status"], _iso(_now()), action_id))
                audit.append("execution.blocked", "system:pulseguard", exc.message,
                             run_id=run["run_id"] if run else None, action_id=action_id,
                             detail={"code": exc.code}, conn=conn)
        raise


def _authorize(action_id: str, retry_execution_id: str | None, failure: dict) -> ExecutionGrant:
    def refuse(code: str, message: str, status: int = 409, *, severity: str = "high", new_status: str | None = None):
        failure.update(severity=severity, status=new_status)
        return GuardError(code, message, status)

    with connect() as conn:
        action = _load_action(conn, action_id)
        now = _now()
        policy = json.loads(action["policy_outcome"])
        if action["status"] not in ("approved", "auto_approved", "executing"):
            raise refuse("not_authorized", f"Action status is '{action['status']}'; only approved actions may execute.")
        stored = json.loads(action["payload"])
        if payload_hash(action["action_type"], stored) != action["payload_hash"]:
            raise refuse("payload_mismatch", "Payload changed after the decision; a new approval is required.",
                         severity="critical", new_status="blocked")
        run = conn.execute("SELECT snapshot_id FROM agent_runs WHERE id=?", (action["run_id"],)).fetchone()
        recheck = evaluate(action["action_type"], stored, Actor(id=action["requested_by"], name="", role="agent", kind="agent"),
                           snapshot_id=run["snapshot_id"])
        if recheck.outcome == "deny":
            raise refuse("policy_denied", "Policy denies execution: " + "; ".join(recheck.reasons), new_status="blocked")
        if recheck.outcome == "require_approval" and policy.get("outcome") == "allow":
            raise refuse("policy_changed", "Policy now requires human approval for this action type.", new_status="blocked")

        existing = conn.execute("SELECT * FROM executions WHERE action_id=?", (action_id,)).fetchone()
        if existing is not None:
            if retry_execution_id != existing["id"] or existing["status"] != "dispatch_failed":
                raise refuse("duplicate_execution", "This action already has an execution; duplicates are rejected.")
            execution_id, idem = existing["id"], existing["idempotency_key"]
        else:
            execution_id, idem = "exe_" + uuid.uuid4().hex[:16], "idem_" + secrets.token_hex(12)

        if policy.get("outcome") != "allow":
            approval = conn.execute("SELECT * FROM approvals WHERE action_id=?", (action_id,)).fetchone()
            if approval is None or approval["decision"] != "approved":
                raise refuse("not_authorized", "No approval record exists for this action.")
            if approval["payload_hash"] != action["payload_hash"]:
                raise refuse("payload_mismatch", "Approved payload differs from the action; a new approval is required.",
                             severity="critical", new_status="blocked")
            if approval["expires_at"] <= _iso(now) and existing is None:
                raise refuse("approval_expired", "The approval expired before execution; approve again.", 410,
                             new_status="expired")
            if approval["consumed_at"] is None:
                cur = conn.execute(
                    "UPDATE approvals SET consumed_at=?, consumed_by_execution=? WHERE action_id=? AND consumed_at IS NULL",
                    (_iso(now), execution_id, action_id))
                if cur.rowcount != 1:
                    raise refuse("replay", "Approval already consumed; replay rejected.")
            elif approval["consumed_by_execution"] != execution_id:
                raise refuse("replay", "Approval already used by another execution; replay rejected.")

        spec = REGISTRY[action["action_type"]]
        if existing is None:
            conn.execute(
                "INSERT INTO executions(id,action_id,idempotency_key,status,attempts,destination,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (execution_id, action_id, idem, "authorized", 0, spec.destination, _iso(now), _iso(now)))
        record_decision(recheck, phase="execution", actor="system:pulseguard", action_id=action_id,
                        run_id=action["run_id"], conn=conn)
        conn.execute("UPDATE actions SET status='executing', updated_at=? WHERE id=?", (_iso(now), action_id))
        audit.append("execution.authorized", "system:pulseguard", f"Execution of {action['action_type']} authorized",
                     run_id=action["run_id"], action_id=action_id, execution_id=execution_id,
                     detail={"payload_hash": action["payload_hash"], "destination": spec.destination}, conn=conn)
        issued = int(time.time())
        unsigned = ExecutionGrant(execution_id, action_id, action["action_type"], stored, action["payload_hash"],
                                  idem, spec.destination, issued, "")
        signature = hmac.new(_grant_key(), unsigned.message(), hashlib.sha256).hexdigest()
        return ExecutionGrant(**{**unsigned.__dict__, "signature": signature})


# ---------------------------------------------------------------------------
# 4. Webhook verification (n8n -> backend)
# ---------------------------------------------------------------------------

def sign(body: bytes, timestamp: int | None = None) -> tuple[str, str]:
    ts = str(timestamp if timestamp is not None else int(time.time()))
    sig = hmac.new(get_settings().webhook_secret(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return ts, sig


def verify_webhook(body: bytes, timestamp: str | None, signature: str | None, *, source: str) -> None:
    settings = get_settings()
    try:
        secret = settings.webhook_secret()
    except RuntimeError:
        raise GuardError("not_configured", "Webhook secret is not configured", 503)
    if not timestamp or not signature or not timestamp.isdigit():
        security_event("webhook_rejected", "high", source, None, {"reason": "missing signature headers"})
        raise GuardError("bad_signature", "Missing signature", 401)
    if abs(time.time() - int(timestamp)) > settings.webhook_max_skew_seconds:
        security_event("webhook_rejected", "high", source, None, {"reason": "timestamp outside allowed skew"})
        raise GuardError("stale", "Stale or future timestamp", 401)
    expected = hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        security_event("webhook_rejected", "high", source, None, {"reason": "invalid signature"})
        raise GuardError("bad_signature", "Invalid signature", 401)


def claim_delivery(delivery_id: str, source: str, execution_id: str | None, conn) -> bool:
    """True the first time a delivery id is seen; False for duplicates."""
    cur = conn.execute("INSERT OR IGNORE INTO webhook_receipts VALUES (?,?,?,?)",
                       (delivery_id[:120], source, execution_id, audit.now_iso()))
    return cur.rowcount == 1
