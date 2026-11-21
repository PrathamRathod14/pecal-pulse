"""Registry of the only action types PulseGuard will ever authorize.

Anything not listed here is denied by default. Payload schemas are strict
(unknown fields rejected), so a model cannot smuggle flags such as
``approved: true`` into an action.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateFollowupTask(_Strict):
    customer_id: str = Field(min_length=1, max_length=40)
    owner: str = Field(min_length=1, max_length=60)
    due_date: date
    note: str = Field(min_length=5, max_length=500)
    priority: Literal["normal", "high"] = "normal"


class NotifyAccountTeam(_Strict):
    customer_id: str = Field(min_length=1, max_length=40)
    team: str = Field(min_length=1, max_length=60)
    message: str = Field(min_length=5, max_length=600)


class SendRetentionEmail(_Strict):
    customer_id: str = Field(min_length=1, max_length=40)
    recipient_email: str = Field(min_length=3, max_length=254)
    subject: str = Field(min_length=3, max_length=120)
    body: str = Field(min_length=20, max_length=2000)
    language: Literal["en", "de"] = "en"


class ScheduleAccountReview(_Strict):
    customer_id: str = Field(min_length=1, max_length=40)
    team: str = Field(min_length=1, max_length=60)
    review_date: date
    agenda: str = Field(min_length=5, max_length=800)


class OfferRetentionDiscount(_Strict):
    customer_id: str = Field(min_length=1, max_length=40)
    discount_pct: float = Field(gt=0, le=100)
    duration_months: int = Field(ge=1, le=24)
    justification: str = Field(min_length=10, max_length=800)


@dataclass(frozen=True)
class ActionSpec:
    action_type: str
    schema: type[_Strict]
    risk: Literal["low", "medium", "high"]
    approval_permission: str | None  # None => eligible for low-risk auto-approval
    required_role: str
    destination: str  # n8n handler key — the only place it may be sent
    external: bool
    label: str


REGISTRY: dict[str, ActionSpec] = {
    spec.action_type: spec
    for spec in [
        ActionSpec("create_followup_task", CreateFollowupTask, "low", None, "analyst",
                   "n8n:followup", False, "Create follow-up task"),
        ActionSpec("notify_account_team", NotifyAccountTeam, "low", None, "analyst",
                   "n8n:notify", False, "Notify account team"),
        ActionSpec("schedule_account_review", ScheduleAccountReview, "medium",
                   "action:approve:standard", "sales_manager", "n8n:review", False,
                   "Schedule account review"),
        ActionSpec("send_retention_email", SendRetentionEmail, "high",
                   "action:approve:standard", "sales_manager", "n8n:email", True,
                   "Send retention e-mail"),
        ActionSpec("offer_retention_discount", OfferRetentionDiscount, "high",
                   "action:approve:financial", "director", "n8n:discount", True,
                   "Offer retention discount"),
    ]
}


def _js_numbers(value):
    """Integral floats as ints so Python and JavaScript canonical JSON match byte for byte."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _js_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_js_numbers(v) for v in value]
    return value


def canonical(value) -> str:
    return json.dumps(_js_numbers(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def payload_hash(action_type: str, payload: dict) -> str:
    return hashlib.sha256(canonical({"action_type": action_type, "payload": payload}).encode()).hexdigest()


def normalize(action_type: str, payload: dict) -> tuple[dict | None, list[str]]:
    """Validate against the strict schema; returns JSON-safe payload or errors."""
    spec = REGISTRY.get(action_type)
    if spec is None:
        return None, [f"unknown action type '{action_type}'"]
    if not isinstance(payload, dict):
        return None, ["payload must be an object"]
    try:
        model = spec.schema.model_validate(payload)
    except ValidationError as exc:
        return None, [
            f"{'.'.join(str(p) for p in err['loc']) or 'payload'}: {err['msg']}" for err in exc.errors()
        ]
    return json.loads(model.model_dump_json()), []
