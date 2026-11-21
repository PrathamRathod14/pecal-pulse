"""HTTP-level tests for /api/pulse: authentication, CSRF, server-side roles,
signed webhooks and live security probes."""

import json
import time
import unittest

from fastapi.testclient import TestClient

from backend.tests.pulse_support import EmulatedN8n, PulseEnv

H = {"X-Pulse-Request": "1"}


class As:
    def __init__(self, client, token):
        self.client, self.cookie = client, {"Cookie": f"pulse_session={token}"}

    def _h(self, headers):
        self.client.cookies.clear()
        return {**(headers or {}), **self.cookie}

    def get(self, url, headers=None):
        return self.client.get(url, headers=self._h(headers))

    def post(self, url, json=None, headers=None):
        return self.client.post(url, json=json, headers=self._h(headers))


class PulseApiTests(unittest.TestCase):
    def setUp(self):
        self.envctx = PulseEnv()
        self.envctx.__enter__()
        from backend.app.main import app
        from backend.app.pulse import n8n

        n8n._transport = EmulatedN8n().transport()
        self.client = TestClient(app)
        self.client.__enter__()  # runs the lifespan (graph runtime, checkpointer)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.envctx.__exit__(None, None, None)

    def login(self, user):
        """A per-user view over the single lifespan-bound client (one event loop)."""
        r = self.client.post("/api/pulse/auth/login", json={"user_id": user}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        token = r.cookies.get("pulse_session")
        self.client.cookies.clear()
        return As(self.client, token)

    def wait(self, c, run_id, statuses):
        for _ in range(200):
            run = c.get(f"/api/pulse/runs/{run_id}").json()
            if run["run"]["status"] in statuses:
                return run
            time.sleep(0.05)
        self.fail(f"run stuck: {run['run']}")

    def test_status_reports_missing_credentials_honestly(self):
        body = self.client.get("/api/pulse/status").json()
        self.assertFalse(body["gemini"]["configured"])
        self.assertEqual(body["gemini"]["missing_env"], "GEMINI_API_KEY")
        self.assertEqual(body["email"]["mode"], "simulated preview")
        self.assertNotIn("test-webhook-secret", json.dumps(body))

    def test_authentication_and_csrf_required(self):
        self.assertEqual(self.client.get("/api/pulse/runs").status_code, 401)
        self.assertEqual(self.client.post("/api/pulse/auth/login", json={"user_id": "jonas.manager"}).status_code, 403)
        self.assertEqual(self.client.post("/api/pulse/auth/login", json={"user_id": "mallory"}, headers=H).status_code, 401)
        self.assertEqual(As(self.client, "forged.token").get("/api/pulse/runs").status_code, 401)

    def test_role_comes_from_server_not_client(self):
        viewer = self.login("sam.viewer")
        r = viewer.post("/api/pulse/runs", json={"customer_id": "DEMO-001", "role": "director"},
                        headers={**H, "X-Role": "director"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(viewer.get("/api/pulse/auth/me").json()["actor"]["role"], "viewer")

    def test_full_api_workflow_and_audit(self):
        analyst, manager = self.login("maria.analyst"), self.login("jonas.manager")
        self.assertEqual(analyst.post("/api/pulse/runs", json={"customer_id": "DEMO-001"}).status_code, 403)  # no CSRF header
        run_id = analyst.post("/api/pulse/runs", json={"customer_id": "DEMO-001"}, headers=H).json()["run_id"]
        self.wait(analyst, run_id, {"awaiting_approval"})
        pending = manager.get("/api/pulse/actions?status=pending_approval").json()["items"]
        self.assertTrue(pending and pending[0]["can_decide"])
        self.assertFalse(analyst.get("/api/pulse/actions?status=pending_approval").json()["items"][0]["can_decide"])
        act = pending[0]
        r = analyst.post(f"/api/pulse/actions/{act['id']}/approve", json={"payload_hash": act["payload_hash"]}, headers=H)
        self.assertEqual(r.status_code, 403)
        r = manager.post(f"/api/pulse/actions/{act['id']}/approve", json={"payload_hash": act["payload_hash"]}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        run = self.wait(manager, run_id, {"completed", "completed_with_failures"})
        self.assertEqual(run["run"]["status"], "completed")
        replay = manager.post(f"/api/pulse/actions/{act['id']}/approve", json={"payload_hash": act["payload_hash"]}, headers=H)
        self.assertEqual(replay.status_code, 409)
        trail = manager.get(f"/api/pulse/audit?run_id={run_id}").json()
        self.assertTrue(trail["chain"]["valid"])
        self.assertIn("approval.granted", [e["kind"] for e in trail["items"]])
        overview = manager.get("/api/pulse/overview").json()
        self.assertGreaterEqual(overview["signals"]["critical"], 1)
        self.assertEqual(self.login("sam.viewer").get("/api/pulse/security/events").status_code, 403)

    def test_unsigned_or_forged_callbacks_rejected(self):
        body = json.dumps({"delivery_id": "x", "execution_id": "exe_x", "status": "succeeded"})
        self.assertEqual(self.client.post("/api/pulse/n8n/callback", content=body).status_code, 401)
        r = self.client.post("/api/pulse/n8n/callback", content=body,
                             headers={"X-Pulse-Timestamp": str(int(time.time())), "X-Pulse-Signature": "0" * 64})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.client.post("/api/pulse/n8n/scan", content=b"{}").status_code, 401)

    def test_live_security_probes_are_all_blocked(self):
        director = self.login("elena.director")
        for probe in ("unknown_action", "unauthorized", "replay", "tamper", "forged_webhook", "duplicate_callback"):
            r = director.post(f"/api/pulse/security/probes/{probe}", headers=H)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()["blocked"], (probe, r.json()))
        self.assertEqual(self.login("jonas.manager").post("/api/pulse/security/probes/replay", headers=H).status_code, 403)

    def test_signed_scan_starts_deduplicated_investigation(self):
        from backend.app.pulse import guard

        body = json.dumps({"max_runs": 1}).encode()
        ts, sig = guard.sign(body)
        first = self.client.post("/api/pulse/n8n/scan", content=body, headers={"X-Pulse-Timestamp": ts, "X-Pulse-Signature": sig}).json()
        self.assertEqual(len(first["started"]), 1)
        ts, sig = guard.sign(body)
        second = self.client.post("/api/pulse/n8n/scan", content=body, headers={"X-Pulse-Timestamp": ts, "X-Pulse-Signature": sig}).json()
        self.assertIn(first["started"][0]["customer_id"], second["already_triggered_today"])


if __name__ == "__main__":
    unittest.main()
