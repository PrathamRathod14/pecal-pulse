"""SQLite persistence for agent runs, actions, approvals, executions and audit.

Versioned, forward-only migrations (``schema_migrations``). Business records
live here; LangGraph checkpoints live in a separate file.

The audit table is append-only *by convention and by trigger*: UPDATE and
DELETE raise. This is a local SQLite file, so anyone with file access can
still alter it — it is tamper-evident through a hash chain, not tamper-proof.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .config import get_settings

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE agent_runs(
            id TEXT PRIMARY KEY,
            objective TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            started_by TEXT NOT NULL,
            scenario TEXT,
            status TEXT NOT NULL,
            stage TEXT,
            llm_source TEXT,
            summary TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_runs_created ON agent_runs(created_at DESC);
        CREATE INDEX idx_runs_customer ON agent_runs(customer_id);

        CREATE TABLE agent_steps(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES agent_runs(id),
            agent TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            input_summary TEXT,
            output TEXT,
            tool_calls TEXT,
            error TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            duration_ms INTEGER
        );
        CREATE INDEX idx_steps_run ON agent_steps(run_id, id);

        CREATE TABLE actions(
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES agent_runs(id),
            customer_id TEXT NOT NULL,
            action_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            rationale TEXT,
            status TEXT NOT NULL,
            risk TEXT,
            required_role TEXT,
            policy_outcome TEXT NOT NULL,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_actions_status ON actions(status);
        CREATE INDEX idx_actions_run ON actions(run_id);

        CREATE TABLE policy_decisions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_id TEXT,
            run_id TEXT,
            phase TEXT NOT NULL,
            actor TEXT NOT NULL,
            action_type TEXT NOT NULL,
            outcome TEXT NOT NULL,
            risk TEXT,
            rules TEXT NOT NULL,
            reasons TEXT NOT NULL,
            payload_hash TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_decisions_created ON policy_decisions(created_at DESC);

        CREATE TABLE approvals(
            id TEXT PRIMARY KEY,
            action_id TEXT NOT NULL UNIQUE REFERENCES actions(id),
            decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
            approver TEXT NOT NULL,
            approver_role TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            comment TEXT,
            decided_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            consumed_by_execution TEXT
        );

        CREATE TABLE executions(
            id TEXT PRIMARY KEY,
            action_id TEXT NOT NULL UNIQUE REFERENCES actions(id),
            idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            n8n_execution_id TEXT,
            destination TEXT,
            result TEXT,
            error TEXT,
            submitted_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_exec_status ON executions(status);

        CREATE TABLE webhook_receipts(
            delivery_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            execution_id TEXT,
            received_at TEXT NOT NULL
        );

        CREATE TABLE security_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            severity TEXT NOT NULL,
            actor TEXT,
            subject TEXT,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_security_created ON security_events(created_at DESC);

        CREATE TABLE audit_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            kind TEXT NOT NULL,
            actor TEXT NOT NULL,
            run_id TEXT,
            action_id TEXT,
            execution_id TEXT,
            summary TEXT NOT NULL,
            detail TEXT,
            prev_hash TEXT NOT NULL,
            hash TEXT NOT NULL
        );
        CREATE INDEX idx_audit_run ON audit_events(run_id);
        CREATE INDEX idx_audit_action ON audit_events(action_id);
        CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events
            BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
        CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events
            BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
        """,
    ),
    (
        2,
        """
        CREATE TABLE signal_triggers(
            customer_id TEXT NOT NULL,
            signal_key TEXT NOT NULL,
            day TEXT NOT NULL,
            run_id TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY(customer_id, signal_key, day)
        );
        """,
    ),
]

_lock = threading.RLock()
_initialized: set[str] = set()


def migrate(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for version, sql in MIGRATIONS:
            if version in applied:
                continue
            with conn:
                conn.executescript("BEGIN;" + sql + f"; INSERT INTO schema_migrations VALUES ({version}, datetime('now')); COMMIT;")
    finally:
        conn.close()


@contextmanager
def connect():
    """Transactional connection: commits on success, rolls back on error."""
    path = get_settings().db_file
    key = str(path)
    with _lock:
        if key not in _initialized:
            migrate(path)
            _initialized.add(key)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def reset_cache() -> None:
    with _lock:
        _initialized.clear()


def schema_version() -> int:
    with connect() as conn:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)
