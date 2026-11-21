"""Append-only, hash-chained audit trail (Audit Agent's persistence layer)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .db import connect

GENESIS = "0" * 64


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _digest(prev: str, record: dict) -> str:
    body = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((prev + body).encode()).hexdigest()


def append(
    kind: str,
    actor: str,
    summary: str,
    *,
    run_id: str | None = None,
    action_id: str | None = None,
    execution_id: str | None = None,
    detail: dict | None = None,
    conn=None,
) -> int:
    """Append one audit event. Pass ``conn`` to join an open transaction."""
    if conn is None:
        with connect() as own:
            return append(kind, actor, summary, run_id=run_id, action_id=action_id,
                          execution_id=execution_id, detail=detail, conn=own)
    prev_row = conn.execute("SELECT hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
    prev = prev_row[0] if prev_row else GENESIS
    record = {
        "at": now_iso(),
        "kind": kind,
        "actor": actor,
        "run_id": run_id,
        "action_id": action_id,
        "execution_id": execution_id,
        "summary": summary,
        "detail": detail or {},
    }
    digest = _digest(prev, record)
    cur = conn.execute(
        "INSERT INTO audit_events(at,kind,actor,run_id,action_id,execution_id,summary,detail,prev_hash,hash)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (record["at"], kind, actor, run_id, action_id, execution_id, summary,
         json.dumps(record["detail"], ensure_ascii=False), prev, digest),
    )
    return int(cur.lastrowid)


def list_events(*, run_id: str | None = None, action_id: str | None = None,
                kind: str | None = None, limit: int = 200, before_id: int | None = None) -> list[dict]:
    clauses, params = [], []
    for column, value in (("run_id", run_id), ("action_id", action_id), ("kind", kind)):
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    if before_id:
        clauses.append("id<?")
        params.append(before_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM audit_events {where} ORDER BY id DESC LIMIT ?", (*params, limit)
        ).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in rows]


def verify_chain() -> dict:
    """Recompute the hash chain; reports the first broken link, if any."""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
    prev = GENESIS
    for r in rows:
        record = {k: r[k] for k in ("at", "kind", "actor", "run_id", "action_id", "execution_id", "summary")}
        record["detail"] = json.loads(r["detail"] or "{}")
        if r["prev_hash"] != prev or _digest(prev, record) != r["hash"]:
            return {"valid": False, "events": len(rows), "broken_at": r["id"]}
        prev = r["hash"]
    return {"valid": True, "events": len(rows), "broken_at": None}
