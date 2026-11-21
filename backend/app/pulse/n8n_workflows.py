"""n8n workflow definitions for PeCal Pulse (source of truth).

``uv run python -m backend.app.pulse.n8n_workflows`` writes the importable
templates to ``n8n/workflows/``. They contain the placeholders
``__PULSE_WEBHOOK_SECRET__`` and ``__PULSE_BACKEND_URL__``, which
``scripts/n8n-setup.sh`` / the Windows launcher fill in at import time (never
committed). HMACs use n8n's built-in Crypto node, so workflows need neither
environment access nor Node built-in modules in Code nodes. Workflows only
ever call the backend (controlled destinations), never a URL from a request.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "n8n/workflows"

EXECUTE_ID = "PulseExecute0001"
SCAN_ID = "PulseRiskScan001"
ERROR_ID = "PulseErrorHdl001"
SECRET = "__PULSE_WEBHOOK_SECRET__"
BACKEND = "__PULSE_BACKEND_URL__"

WORKFLOWS = [
    {"id": EXECUTE_ID, "name": "SignalPilot – Execute approved action", "trigger": "Webhook POST /webhook/pulse-execute (HMAC)",
     "purpose": "Verifies the PulseGuard-signed request, executes the authorized action and sends a signed callback with the verified result.",
     "handlers": {"create_followup_task": "creates a real follow-up via the PeCal API",
                  "schedule_account_review": "creates a review follow-up via the PeCal API + .ics invite text",
                  "notify_account_team": "simulated (no chat provider configured) — preview recorded",
                  "send_retention_email": "simulated (no SMTP configured) — preview recorded",
                  "offer_retention_discount": "simulated (no billing system) — recorded for manual entry"}},
    {"id": SCAN_ID, "name": "SignalPilot – Risk-triggered investigations", "trigger": "Schedule (every 6 h) or webhook POST /webhook/pulse-scan (HMAC)",
     "purpose": "Asks PeCal Pulse to scan for new critical signals and start investigations (deduplicated per customer per day)."},
    {"id": ERROR_ID, "name": "SignalPilot – Error handler", "trigger": "n8n Error Trigger",
     "purpose": "Reports unexpected workflow failures to PeCal Pulse so executions never stay 'submitted' silently."},
]

CANON_JS = """const canon = (v) => Array.isArray(v) ? '[' + v.map(canon).join(',') + ']'
  : (v !== null && typeof v === 'object') ? '{' + Object.keys(v).sort().map(k => JSON.stringify(k) + ':' + canon(v[k])).join(',') + '}'
  : JSON.stringify(v);
const safeEqual = (a, b) => { if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let d = 0; for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i); return d === 0; };
"""

PREPARE_VERIFY_JS = CANON_JS + """
const item = $input.first().json;
const headers = item.headers || {};
const ts = String(headers['x-pulse-timestamp'] || '');
return [{ json: { ts, sig: String(headers['x-pulse-signature'] || ''), body: item.body || {}, message: ts + '.' + canon(item.body || {}) } }];
"""

VERIFY_JS = CANON_JS + """
const j = $input.first().json;
const body = j.body || {};
let valid = false, reason = 'ok';
if (!/^[0-9]+$/.test(j.ts) || !/^[0-9a-f]{64}$/.test(j.sig)) { reason = 'missing signature'; }
else if (Math.abs(Date.now() / 1000 - Number(j.ts)) > 300) { reason = 'stale timestamp'; }
else if (!safeEqual(j.expected, j.sig)) { reason = 'invalid signature'; }
else { valid = true; }
const allowed = ['create_followup_task','notify_account_team','send_retention_email','schedule_account_review','offer_retention_discount'];
if (valid && !allowed.includes(body.action_type)) { valid = false; reason = 'action type not allowed by workflow'; }
if (valid && !(body.execution_id && body.idempotency_key && body.payload_hash && body.payload)) { valid = false; reason = 'incomplete envelope'; }
return [{ json: { valid, reason, ...(valid ? body : {}), n8n_execution_id: $execution.id } }];
"""

SCAN_VERIFY_JS = CANON_JS + """
const j = $input.first().json;
const ok = /^[0-9]+$/.test(j.ts) && Math.abs(Date.now() / 1000 - Number(j.ts)) <= 300 && safeEqual(j.expected, j.sig);
if (!ok) throw new Error('Rejected unsigned scan trigger');
return [{ json: { ok: true } }];
"""

ROUTE_EXPR = ("={{ ({create_followup_task:0, schedule_account_review:1, notify_account_team:2, "
              "send_retention_email:3, offer_retention_discount:4})[$json.action_type] }}")

FOLLOWUP_BODY = """={{ JSON.stringify({
  customer_id: $json.payload.customer_id,
  owner: $json.payload.owner,
  due_date: $json.payload.due_date,
  note: '[Pulse ' + $json.execution_id + '] ' + $json.payload.note,
  outcome: 'Timing to confirm',
  request_key: $json.idempotency_key
}) }}"""

REVIEW_BODY = """={{ JSON.stringify({
  customer_id: $json.payload.customer_id,
  owner: $json.payload.team,
  due_date: $json.payload.review_date,
  note: '[Pulse ' + $json.execution_id + '] Account review: ' + $json.payload.agenda,
  outcome: 'Need confirmed',
  request_key: $json.idempotency_key
}) }}"""

FOLLOWUP_RESULT_JS = """
const env = $('Verify signature').first().json;
const created = $input.first().json;
const task = (created.payload && created.payload.task) || created.task || created.payload || {};
const result = { delivery: 'created', system: 'PeCal follow-ups API', followup_id: task.id || null, owner: env.payload.owner || env.payload.team };
if (env.action_type === 'schedule_account_review') {
  const d = env.payload.review_date.replace(/-/g, '');
  result.calendar = 'no calendar provider configured — invite rendered as .ics text';
  result.ics = ['BEGIN:VCALENDAR','VERSION:2.0','BEGIN:VEVENT','DTSTART;VALUE=DATE:' + d,'SUMMARY:Account review ' + env.payload.customer_id,'DESCRIPTION:' + env.payload.agenda.replace(/\\n/g,' '),'END:VEVENT','END:VCALENDAR'].join('\\r\\n');
}
return [{ json: { ...env, result } }];
"""

SIMULATED_JS = """
const env = $input.first().json;
const p = env.payload;
const previews = {
  notify_account_team: { channel: 'internal team feed', team: p.team, message: p.message,
                         reason: 'No chat provider (Slack/Teams) configured in n8n; notification recorded as a preview.' },
  send_retention_email: { to: p.recipient_email, subject: p.subject, body: p.body, language: p.language,
                          reason: 'No SMTP credential configured and external delivery disabled; nothing was sent.' },
  offer_retention_discount: { discount_pct: p.discount_pct, duration_months: p.duration_months, justification: p.justification,
                              reason: 'No billing system connected; offer recorded for manual entry.' },
};
return [{ json: { ...env, result: { delivery: 'simulated', preview: previews[env.action_type] } } }];
"""

CALLBACK_JS = """
const out = [];
for (const it of $input.all()) {
  const j = it.json;
  const env = j.execution_id ? j : $('Verify signature').first().json;
  const failed = !!j.error || !j.result;
  const msg = {
    delivery_id: 'n8n-' + $execution.id + '-' + env.execution_id,
    execution_id: env.execution_id,
    idempotency_key: env.idempotency_key,
    payload_hash: env.payload_hash,
    n8n_execution_id: String($execution.id),
    status: failed ? 'failed' : 'succeeded',
    result: failed ? {} : j.result,
    error: failed ? String((j.error && (j.error.message || j.error)) || 'handler produced no result').slice(0, 400) : null,
  };
  const body = JSON.stringify(msg);
  const ts = String(Math.floor(Date.now() / 1000));
  out.push({ json: { body, ts, message: ts + '.' + body } });
}
return out;
"""

SCAN_JS = """
const body = JSON.stringify({ max_runs: 1, requested_by: 'n8n', n8n_execution_id: String($execution.id) });
const ts = String(Math.floor(Date.now() / 1000));
return [{ json: { body, ts, message: ts + '.' + body } }];
"""


ERROR_JS = """
const e = $input.first().json;
const body = JSON.stringify({
  failed_n8n_execution_id: String((e.execution && e.execution.id) || ''),
  workflow: (e.workflow && e.workflow.name) || null,
  node: (e.execution && e.execution.lastNodeExecuted) || null,
  message: String((e.execution && e.execution.error && e.execution.error.message) || 'unknown error').slice(0, 400),
});
const ts = String(Math.floor(Date.now() / 1000));
return [{ json: { body, ts, message: ts + '.' + body } }];
"""


def _signed_post(name: str, path: str, pos: list[int]) -> dict:
    return {
        "id": name.lower().replace(" ", "-"), "name": name, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": pos,
        "parameters": {
            "method": "POST", "url": BACKEND + path,
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "X-Pulse-Timestamp", "value": "={{ $json.ts }}"},
                {"name": "X-Pulse-Signature", "value": "={{ $json.sig }}"},
                {"name": "Content-Type", "value": "application/json"},
            ]},
            "sendBody": True, "contentType": "raw", "rawContentType": "application/json", "body": "={{ $json.body }}",
            "options": {"timeout": 15000},
        },
        "retryOnFail": True, "maxTries": 3, "waitBetweenTries": 2000,
    }


def _hmac(name: str, pos: list[int], field: str) -> dict:
    """n8n built-in Crypto node (v1): HMAC-SHA256 of $json.message into `field`."""
    return {"id": name.lower().replace(" ", "-"), "name": name, "type": "n8n-nodes-base.crypto", "typeVersion": 1,
            "position": pos, "parameters": {"action": "hmac", "type": "SHA256", "value": "={{ $json.message }}",
                                             "dataPropertyName": field, "secret": SECRET, "encoding": "hex"}}


def _code(name: str, js: str, pos: list[int], **extra) -> dict:
    return {"id": name.lower().replace(" ", "-"), "name": name, "type": "n8n-nodes-base.code", "typeVersion": 2,
            "position": pos, "parameters": {"mode": "runOnceForAllItems", "jsCode": js.strip()}, **extra}


def _followup_http(name: str, body: str, pos: list[int]) -> dict:
    return {
        "id": name.lower().replace(" ", "-"), "name": name, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": pos, "onError": "continueErrorOutput",
        "parameters": {"method": "POST", "url": BACKEND + "/api/v2/followups",
                       "sendBody": True, "contentType": "raw", "rawContentType": "application/json", "body": body,
                       "options": {"timeout": 10000}},
    }


def execute_workflow() -> dict:
    nodes = [
        {"id": "webhook", "name": "Pulse execute webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2,
         "position": [0, 300], "webhookId": "5d1c8a52-pulse-execute",
         "parameters": {"httpMethod": "POST", "path": "pulse-execute", "responseMode": "responseNode", "options": {}}},
        _code("Prepare verification", PREPARE_VERIFY_JS, [160, 300]),
        _hmac("Compute HMAC", [320, 300], "expected"),
        _code("Verify signature", VERIFY_JS, [480, 300]),
        {"id": "if-valid", "name": "Signature valid?", "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": [440, 300],
         "parameters": {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                                       "conditions": [{"id": "c1", "leftValue": "={{ $json.valid }}", "rightValue": True,
                                                       "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
                                       "combinator": "and"}, "options": {}}},
        {"id": "accept", "name": "Respond 202 accepted", "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.1,
         "position": [660, 200],
         "parameters": {"respondWith": "json",
                        "responseBody": "={{ JSON.stringify({ accepted: true, n8n_execution_id: String($execution.id), execution_id: $json.execution_id }) }}",
                        "options": {"responseCode": 202}}},
        {"id": "deny", "name": "Respond 401 rejected", "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.1,
         "position": [660, 420],
         "parameters": {"respondWith": "json", "responseBody": "={{ JSON.stringify({ accepted: false, reason: $json.reason }) }}",
                        "options": {"responseCode": 401}}},
        {"id": "route", "name": "Route by action type", "type": "n8n-nodes-base.switch", "typeVersion": 3.2, "position": [880, 200],
         "parameters": {"mode": "expression", "numberOutputs": 5, "output": ROUTE_EXPR}},
        _followup_http("Create follow-up (PeCal API)", FOLLOWUP_BODY, [1120, 0]),
        _followup_http("Create review task (PeCal API)", REVIEW_BODY, [1120, 160]),
        _code("Follow-up result", FOLLOWUP_RESULT_JS, [1360, 80]),
        _code("Simulated delivery (no provider)", SIMULATED_JS, [1120, 380]),
        _code("Build signed callback", CALLBACK_JS, [1600, 240]),
        _hmac("Sign callback", [1760, 240], "sig"),
        _signed_post("Callback to SignalPilot", "/api/pulse/n8n/callback", [1920, 240]),
    ]
    c = lambda node, idx=0: {"node": node, "type": "main", "index": idx}
    connections = {
        "Pulse execute webhook": {"main": [[c("Prepare verification")]]},
        "Prepare verification": {"main": [[c("Compute HMAC")]]},
        "Compute HMAC": {"main": [[c("Verify signature")]]},
        "Verify signature": {"main": [[c("Signature valid?")]]},
        "Signature valid?": {"main": [[c("Respond 202 accepted")], [c("Respond 401 rejected")]]},
        "Respond 202 accepted": {"main": [[c("Route by action type")]]},
        "Route by action type": {"main": [
            [c("Create follow-up (PeCal API)")], [c("Create review task (PeCal API)")],
            [c("Simulated delivery (no provider)")], [c("Simulated delivery (no provider)")], [c("Simulated delivery (no provider)")]]},
        "Create follow-up (PeCal API)": {"main": [[c("Follow-up result")], [c("Build signed callback")]]},
        "Create review task (PeCal API)": {"main": [[c("Follow-up result")], [c("Build signed callback")]]},
        "Follow-up result": {"main": [[c("Build signed callback")]]},
        "Simulated delivery (no provider)": {"main": [[c("Build signed callback")]]},
        "Build signed callback": {"main": [[c("Sign callback")]]},
        "Sign callback": {"main": [[c("Callback to SignalPilot")]]},
    }
    return {"id": EXECUTE_ID, "name": WORKFLOWS[0]["name"], "active": True, "nodes": nodes, "connections": connections,
            "settings": {"executionOrder": "v1", "errorWorkflow": ERROR_ID, "saveDataSuccessExecution": "all",
                         "saveDataErrorExecution": "all", "executionTimeout": 120}, "pinData": {}, "tags": []}


def scan_workflow() -> dict:
    nodes = [
        {"id": "schedule", "name": "Every 6 hours", "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2,
         "position": [0, 100], "parameters": {"rule": {"interval": [{"field": "hours", "hoursInterval": 6}]}}},
        {"id": "scan-webhook", "name": "Scan now webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2,
         "position": [0, 320], "webhookId": "7a2f9c10-pulse-scan",
         "parameters": {"httpMethod": "POST", "path": "pulse-scan", "responseMode": "lastNode", "options": {}}},
        _code("Prepare scan verification", PREPARE_VERIFY_JS, [180, 320]),
        _hmac("Compute scan HMAC", [340, 320], "expected"),
        _code("Verify scan trigger", SCAN_VERIFY_JS, [500, 320]),
        _code("Build scan request", SCAN_JS, [660, 200]),
        _hmac("Sign scan request", [820, 200], "sig"),
        _signed_post("Ask SignalPilot to investigate", "/api/pulse/n8n/scan", [980, 200]),
    ]
    c = lambda node: {"node": node, "type": "main", "index": 0}
    connections = {
        "Every 6 hours": {"main": [[c("Build scan request")]]},
        "Scan now webhook": {"main": [[c("Prepare scan verification")]]},
        "Prepare scan verification": {"main": [[c("Compute scan HMAC")]]},
        "Compute scan HMAC": {"main": [[c("Verify scan trigger")]]},
        "Verify scan trigger": {"main": [[c("Build scan request")]]},
        "Build scan request": {"main": [[c("Sign scan request")]]},
        "Sign scan request": {"main": [[c("Ask SignalPilot to investigate")]]},
    }
    return {"id": SCAN_ID, "name": WORKFLOWS[1]["name"], "active": True, "nodes": nodes, "connections": connections,
            "settings": {"executionOrder": "v1", "errorWorkflow": ERROR_ID}, "pinData": {}, "tags": []}


def error_workflow() -> dict:
    nodes = [
        {"id": "error-trigger", "name": "On workflow error", "type": "n8n-nodes-base.errorTrigger", "typeVersion": 1,
         "position": [0, 200], "parameters": {}},
        _code("Build failure report", ERROR_JS, [220, 200]),
        _hmac("Sign failure report", [380, 200], "sig"),
        _signed_post("Report failure to SignalPilot", "/api/pulse/n8n/error", [540, 200]),
    ]
    c = lambda node: {"node": node, "type": "main", "index": 0}
    connections = {"On workflow error": {"main": [[c("Build failure report")]]},
                   "Build failure report": {"main": [[c("Sign failure report")]]},
                   "Sign failure report": {"main": [[c("Report failure to SignalPilot")]]}}
    return {"id": ERROR_ID, "name": WORKFLOWS[2]["name"], "active": True, "nodes": nodes, "connections": connections,
            "settings": {"executionOrder": "v1"}, "pinData": {}, "tags": []}


def write() -> list[Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, wf in (("pulse-execute-action", execute_workflow()), ("pulse-risk-scan", scan_workflow()),
                     ("pulse-error-handler", error_workflow())):
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        paths.append(path)
    return paths


if __name__ == "__main__":
    for p in write():
        print(p.relative_to(ROOT))
