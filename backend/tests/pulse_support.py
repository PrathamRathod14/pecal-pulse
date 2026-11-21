"""Shared isolation for PeCal Pulse tests: temp databases, fixed secrets,
the deterministic synthetic demo snapshot and an emulated n8n transport."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

SNAPSHOT = "synthetic-pulse-demo"
SECRET = "test-webhook-secret-0123456789abcdef"


class PulseEnv:
    def __init__(self, **overrides):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        env = {
            "PULSE_DB_PATH": str(root / "pulse.sqlite3"),
            "PULSE_CHECKPOINT_PATH": str(root / "checkpoints.sqlite3"),
            "PECAL_DEMO_DB": str(root / "demo.sqlite3"),
            "PULSE_SNAPSHOT": SNAPSHOT,
            "PULSE_WEBHOOK_SECRET": SECRET,
            "PULSE_SESSION_SECRET": "test-session-secret",
            "GEMINI_API_KEY": "",
            "PULSE_AUTH_MODE": "demo",
            "PULSE_DEMO_MODE": "true",
            "PULSE_ALLOW_EXTERNAL_EMAIL": "false",
            "PULSE_AUTO_APPROVE_LOW_RISK": "true",
            "PULSE_APPROVAL_TTL_SECONDS": "900",
            "N8N_BASE_URL": "http://n8n.test",
            **overrides,
        }
        self.patch = patch.dict(os.environ, env)

    def __enter__(self):
        from backend.app.pulse import config, db, demo_data, tools

        demo_data.write()
        self.patch.start()
        config.reset_settings()
        db.reset_cache()
        tools._crm.cache_clear()
        return self

    def __exit__(self, *exc):
        from backend.app.pulse import config, db, n8n

        n8n._transport = None
        self.patch.stop()
        config.reset_settings()
        db.reset_cache()
        self.tmp.cleanup()


class EmulatedN8n:
    """Behaves like the imported n8n workflow: verifies the HMAC, 'executes',
    then posts a signed callback (synchronously, before replying — the
    race the dispatcher must tolerate)."""

    def __init__(self, *, fail_types: set[str] = frozenset(), callback: bool = True, status_code: int = 202):
        self.received: list[dict] = []
        self.fail_types = set(fail_types)
        self.callback = callback
        self.status_code = status_code

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        from backend.app.pulse import guard, service

        if request.url.port == 9:  # the n8n-outage scenario's unreachable address
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        body = request.content
        try:
            guard.verify_webhook(body, request.headers.get("x-pulse-timestamp"),
                                 request.headers.get("x-pulse-signature"), source="test-n8n")
        except guard.GuardError as exc:
            return httpx.Response(401, json={"accepted": False, "reason": exc.message})
        envelope = json.loads(body)
        self.received.append(envelope)
        if self.status_code >= 300:
            return httpx.Response(self.status_code, json={"accepted": False})
        n8n_id = str(len(self.received))
        if self.callback:
            failed = envelope["action_type"] in self.fail_types
            msg = {"delivery_id": f"n8n-{n8n_id}-{envelope['execution_id']}", "execution_id": envelope["execution_id"],
                   "idempotency_key": envelope["idempotency_key"], "payload_hash": envelope["payload_hash"],
                   "n8n_execution_id": n8n_id, "status": "failed" if failed else "succeeded",
                   "result": {} if failed else {"delivery": "simulated", "handler": envelope["destination"]},
                   "error": "handler failed" if failed else None}
            raw = json.dumps(msg).encode()
            ts, sig = guard.sign(raw)
            service.handle_callback(raw, ts, sig)
        return httpx.Response(self.status_code, json={"accepted": True, "n8n_execution_id": n8n_id})
