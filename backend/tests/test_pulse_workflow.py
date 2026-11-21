"""End-to-end LangGraph workflow tests with an emulated n8n and a fake Gemini."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.tests.pulse_support import EmulatedN8n, PulseEnv


async def wait_status(run_id, statuses, timeout=15.0):
    from backend.app.pulse.db import connect

    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        with connect() as conn:
            row = conn.execute("SELECT status, error FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        if row and row["status"] in statuses:
            return row["status"]
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"run {run_id} stuck in {dict(row) if row else None}")
        await asyncio.sleep(0.05)


def actions(run_id):
    from backend.app.pulse.db import connect

    with connect() as conn:
        return {r["action_type"]: dict(r) for r in conn.execute(
            "SELECT a.*, e.status AS exec_status, e.id AS exec_id FROM actions a LEFT JOIN executions e ON e.action_id=a.id WHERE a.run_id=?",
            (run_id,))}


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.envctx = PulseEnv()
        self.envctx.__enter__()
        from backend.app.pulse import auth, guard, n8n, service

        self.n8n_mod, self.service, self.guard = n8n, service, guard
        self.fake = EmulatedN8n()
        n8n._transport = self.fake.transport()
        self.analyst = auth._user("maria.analyst")
        self.manager = auth._user("jonas.manager")
        self.lifespan = service.pulse_lifespan()
        self.rt = await self.lifespan.__aenter__()

    async def asyncTearDown(self):
        await self.lifespan.__aexit__(None, None, None)
        self.envctx.__exit__(None, None, None)

    async def start(self, customer="DEMO-001", scenario="standard"):
        return await self.rt.start(self.analyst, customer, "Investigate risk", scenario)

    def pending(self, run_id):
        return actions(run_id)["send_retention_email"]

    async def test_successful_end_to_end_workflow(self):
        run_id = await self.start()
        self.assertEqual(await wait_status(run_id, {"awaiting_approval", "failed"}), "awaiting_approval")
        acts = actions(run_id)
        self.assertEqual(acts["create_followup_task"]["status"], "auto_approved")
        self.assertIsNone(acts["create_followup_task"]["exec_id"], "nothing executes before the gate")
        email = acts["send_retention_email"]
        self.assertEqual(email["status"], "pending_approval")
        self.assertEqual(self.fake.received, [])
        self.guard.approve(self.manager, email["id"], email["payload_hash"])
        self.assertTrue(await self.rt.maybe_resume(run_id))
        self.assertEqual(await wait_status(run_id, {"completed", "completed_with_failures", "failed"}), "completed")
        acts = actions(run_id)
        self.assertEqual({a["exec_status"] for a in acts.values()}, {"succeeded"})
        self.assertEqual({e["action_type"] for e in self.fake.received}, {"create_followup_task", "send_retention_email"})
        sent = next(e for e in self.fake.received if e["action_type"] == "send_retention_email")
        self.assertEqual(sent["payload_hash"], email["payload_hash"], "n8n receives exactly the approved payload")
        from backend.app.pulse import audit
        kinds = [e["kind"] for e in reversed(audit.list_events(run_id=run_id))]
        for expected in ("run.started", "run.planned", "strategy.recommended", "action.pending_approval",
                         "approval.granted", "execution.authorized", "execution.submitted", "execution.succeeded", "run.completed"):
            self.assertIn(expected, kinds)
        from backend.app.pulse.db import connect
        with connect() as conn:
            agents = [r[0] for r in conn.execute("SELECT agent FROM agent_steps WHERE run_id=? ORDER BY id", (run_id,))]
            run = conn.execute("SELECT llm_source FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        for agent in ("orchestrator", "customer_intelligence", "risk_detection", "opportunity_intelligence",
                      "strategy", "pulseguard", "approval_gate", "workflow_execution", "audit"):
            self.assertIn(agent, agents)
        self.assertEqual(run["llm_source"], "deterministic_fallback")  # no key in tests: honestly labelled

    async def test_rejected_action_never_executes(self):
        run_id = await self.start()
        await wait_status(run_id, {"awaiting_approval"})
        self.guard.reject(self.manager, self.pending(run_id)["id"], "not now")
        await self.rt.maybe_resume(run_id)
        await wait_status(run_id, {"completed", "completed_with_failures"})
        self.assertEqual([e["action_type"] for e in self.fake.received], ["create_followup_task"])
        self.assertIsNone(self.pending(run_id)["exec_id"])

    async def test_unauthorized_approval_does_not_resume(self):
        run_id = await self.start()
        await wait_status(run_id, {"awaiting_approval"})
        email = self.pending(run_id)
        with self.assertRaises(self.guard.GuardError):
            self.guard.approve(self.analyst, email["id"], email["payload_hash"])
        self.assertFalse(await self.rt.maybe_resume(run_id))
        self.assertEqual(self.fake.received, [])

    async def test_expired_approval_scenario(self):
        run_id = await self.start(scenario="short_expiry")
        await wait_status(run_id, {"awaiting_approval"})
        from backend.app.pulse.db import connect
        with connect() as conn:  # fast-forward the 45 s demo TTL
            conn.execute("UPDATE actions SET expires_at='2000-01-01T00:00:00+00:00' WHERE run_id=? AND status='pending_approval'", (run_id,))
        await self.rt.sweep()
        await wait_status(run_id, {"completed", "completed_with_failures"})
        self.assertEqual(self.pending(run_id)["status"], "expired")
        self.assertNotIn("send_retention_email", [e["action_type"] for e in self.fake.received])

    async def test_gemini_unavailable_uses_labelled_fallback(self):
        run_id = await self.start(scenario="gemini_unavailable")
        await wait_status(run_id, {"awaiting_approval"})
        from backend.app.pulse.db import connect
        with connect() as conn:
            out = json.loads(conn.execute("SELECT output FROM agent_steps WHERE run_id=? AND agent='strategy'", (run_id,)).fetchone()[0])
        self.assertEqual(out["strategy"]["llm"]["source"], "deterministic_fallback")
        self.assertIn("forced", out["strategy"]["llm"]["category"])

    async def test_n8n_unavailable_then_retry(self):
        run_id = await self.start(scenario="n8n_unavailable")
        await wait_status(run_id, {"awaiting_approval"})
        email = self.pending(run_id)
        self.guard.approve(self.manager, email["id"], email["payload_hash"])
        await self.rt.maybe_resume(run_id)
        self.assertEqual(await wait_status(run_id, {"completed_with_failures", "completed"}), "completed_with_failures")
        acts = actions(run_id)
        self.assertEqual({a["exec_status"] for a in acts.values()}, {"dispatch_failed"})
        self.assertEqual(self.fake.received, [])
        # Retry the same execution against the (now reachable) n8n: same idempotency key, approval reused once.
        await self.rt.retry_execution(self.manager, acts["send_retention_email"]["exec_id"])
        self.assertEqual(actions(run_id)["send_retention_email"]["exec_status"], "succeeded")
        with self.assertRaises(self.guard.GuardError):  # cannot retry a finished execution
            await self.rt.retry_execution(self.manager, acts["send_retention_email"]["exec_id"])

    async def test_failed_execution_is_reported(self):
        self.fake.fail_types = {"create_followup_task"}
        run_id = await self.start()
        await wait_status(run_id, {"awaiting_approval"})
        self.guard.reject(self.manager, self.pending(run_id)["id"])
        await self.rt.maybe_resume(run_id)
        self.assertEqual(await wait_status(run_id, {"completed_with_failures", "completed"}), "completed_with_failures")
        self.assertEqual(actions(run_id)["create_followup_task"]["status"], "execution_failed")

    async def test_prompt_injection_cannot_bypass_policy(self):
        run_id = await self.start(customer="DEMO-007", scenario="prompt_injection")
        await wait_status(run_id, {"awaiting_approval", "completed", "completed_with_failures"})
        from backend.app.pulse.db import connect
        with connect() as conn:
            rows = conn.execute("SELECT action_type, status, rationale FROM actions WHERE run_id=?", (run_id,)).fetchall()
        adversarial = [r for r in rows if r["rationale"].startswith("ADVERSARIAL")]
        self.assertEqual(len(adversarial), 3)
        self.assertTrue(all(r["status"] == "blocked" for r in adversarial))
        with connect() as conn:
            pending = conn.execute("SELECT id, payload_hash FROM actions WHERE run_id=? AND status='pending_approval'", (run_id,)).fetchall()
        for r in pending:
            self.guard.approve(self.manager, r["id"], r["payload_hash"])
        await self.rt.maybe_resume(run_id)
        await wait_status(run_id, {"completed", "completed_with_failures"})
        self.assertFalse(any(e["action_type"] == "export_customer_data" for e in self.fake.received))
        self.assertFalse(any(e["payload"].get("recipient_email") == "attacker@evil.test" for e in self.fake.received))

    async def test_paused_run_survives_restart(self):
        run_id = await self.start()
        await wait_status(run_id, {"awaiting_approval"})
        while run_id in self.rt.tasks:  # let the pause checkpoint land
            await asyncio.sleep(0.05)
        await self.lifespan.__aexit__(None, None, None)  # simulate API restart
        self.lifespan = self.service.pulse_lifespan()
        self.rt = await self.lifespan.__aenter__()
        email = self.pending(run_id)
        self.guard.approve(self.manager, email["id"], email["payload_hash"])
        snap = await self.rt.graph.aget_state({"configurable": {"thread_id": run_id}})
        self.assertEqual(snap.next, ("approval_gate",), "checkpoint restored after restart")
        self.assertTrue(await self.rt.maybe_resume(run_id))
        self.assertEqual(await wait_status(run_id, {"completed", "completed_with_failures"}), "completed")

    async def test_unknown_customer_is_rejected(self):
        with self.assertRaises(LookupError):
            await self.start(customer="NOPE-404")

    async def test_gemini_structured_output_is_used_and_validated(self):
        from backend.app.pulse import config, gemini

        valid = {
            "summary": "Nordwerk volume collapsed.", "risk_assessment": "High", "confidence": 0.77,
            "confidence_rationale": "Strong measured drop", "expected_impact": "Protect upcoming due calibrations",
            "recommendations": ["Call this week"], "measured_facts_used": ["-75%"], "assumptions": ["cause unknown"],
            "proposed_actions": [{"action_type": "notify_account_team", "rationale": "inform team",
                                  "message": "Nordwerk volume dropped sharply; please call."}],
        }
        calls = []

        class Models:
            async def generate_content(self, model, contents, config):
                calls.append(model)
                if len(calls) == 1:
                    return SimpleNamespace(text="{not json", usage_metadata=None)  # malformed → retried
                return SimpleNamespace(text=json.dumps(valid), usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=20))

        fake_client = SimpleNamespace(aio=SimpleNamespace(models=Models()))
        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}):
            config.reset_settings()
            with patch.object(gemini, "_get_client", return_value=fake_client):
                run_id = await self.start()
                await wait_status(run_id, {"completed", "awaiting_approval", "completed_with_failures"})
        from backend.app.pulse.db import connect
        with connect() as conn:
            run = conn.execute("SELECT llm_source, summary FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(run["llm_source"], "gemini")
        self.assertEqual(run["summary"], "Nordwerk volume collapsed.")
        self.assertEqual(len(calls), 2)

    async def test_gemini_errors_fall_back_without_claiming_gemini(self):
        from google.genai import errors

        from backend.app.pulse import config, gemini

        class Models:
            async def generate_content(self, model, contents, config):
                raise errors.APIError(403, {"error": {"message": "denied"}})

        with patch.dict("os.environ", {"GEMINI_API_KEY": "bad-key"}):
            config.reset_settings()
            with patch.object(gemini, "_get_client", return_value=SimpleNamespace(aio=SimpleNamespace(models=Models()))):
                run_id = await self.start()
                await wait_status(run_id, {"awaiting_approval", "completed"})
        from backend.app.pulse.db import connect
        with connect() as conn:
            out = json.loads(conn.execute("SELECT output FROM agent_steps WHERE run_id=? AND agent='strategy'", (run_id,)).fetchone()[0])
        self.assertEqual(out["strategy"]["llm"]["source"], "deterministic_fallback")
        self.assertEqual(out["strategy"]["llm"]["category"], "auth")


def conn_action(run_id, action_type):
    a = actions(run_id)[action_type]
    return a["id"], a["payload_hash"]


if __name__ == "__main__":
    unittest.main()
