"""Read-only data tools the agents call. Each call is traced.

Tools read the active snapshot through the existing data capability and the
synthetic demo CRM file. Agents get no write tools: the only path to a side
effect is a proposed action that PulseGuard authorizes.
"""

from __future__ import annotations

import json
import statistics
import time
from functools import lru_cache
from pathlib import Path

from ..capabilities import data
from .config import get_settings

ROOT = Path(__file__).resolve().parents[3]
CRM_FILE = ROOT / "data/mock/pulse_demo_crm.json"


@lru_cache(maxsize=4)
def _crm(snapshot_id: str) -> dict:
    try:
        crm = json.loads(CRM_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"contacts": {}, "notes": {}, "account_owners": {}, "synthetic": True}
    # Demo CRM only applies to the demo snapshot it was generated for.
    if crm.get("snapshot_id") != snapshot_id:
        return {"contacts": {}, "notes": {}, "account_owners": {}, "synthetic": True}
    return crm


def crm(snapshot_id: str | None = None) -> dict:
    return _crm(snapshot_id or get_settings().snapshot_id)


class ToolRecorder:
    """Collects tool calls for the execution trace."""

    def __init__(self):
        self.calls: list[dict] = []

    def call(self, name: str, fn, **kwargs):
        start = time.perf_counter()
        try:
            result = fn(**kwargs)
            self.calls.append({"tool": name, "args": kwargs, "ok": True,
                               "ms": round((time.perf_counter() - start) * 1000, 1),
                               "result_summary": _summarize(result)})
            return result
        except Exception as exc:  # recorded, then re-raised for the node's retry policy
            self.calls.append({"tool": name, "args": kwargs, "ok": False,
                               "ms": round((time.perf_counter() - start) * 1000, 1),
                               "error": f"{type(exc).__name__}: {exc}"[:300]})
            raise


def _summarize(result) -> str:
    if isinstance(result, list):
        return f"{len(result)} rows"
    if isinstance(result, dict):
        return ", ".join(f"{k}={result[k]}" for k in list(result)[:5] if not isinstance(result[k], (list, dict)))[:200] or f"{len(result)} fields"
    return str(result)[:200]


# ---- tools -----------------------------------------------------------------

def customer_exists(snapshot_id: str, customer_id: str) -> bool:
    return any(p.customer_id == customer_id for p in data.list_customers(snapshot_id))


def get_profile(snapshot_id: str, customer_id: str) -> dict:
    detail = data.get_customer_detail(snapshot_id, customer_id)
    p = detail["profile"]
    return {"customer_id": p.customer_id, "display_name": p.display_name,
            "industry": p.industry_label, "name_source": p.name_source}


def get_monthly_history(snapshot_id: str, customer_id: str) -> list[dict]:
    detail = data.get_customer_detail(snapshot_id, customer_id)
    return [{"month": h.month, "calibrations": h.calibration_events,
             "instruments": h.distinct_instruments} for h in sorted(detail["history"], key=lambda h: h.month)]


def get_portfolio(snapshot_id: str, customer_id: str) -> list[dict]:
    detail = data.get_customer_detail(snapshot_id, customer_id)
    return [{"equipment_group": p.group_label, "calibrations_12m": p.calibration_events}
            for p in detail["portfolio"]]


def get_due_requirements(snapshot_id: str, customer_id: str) -> dict:
    detail = data.get_customer_detail(snapshot_id, customer_id)
    reference = data.get_manifest(snapshot_id).reference_date
    eligible = [r for r in detail["requirements"] if r.eligibility != "excluded" and r.window_end]
    overdue = [r for r in eligible if r.window_end < reference]
    upcoming = [r for r in eligible if r.window_end >= reference]
    return {"reference_date": reference, "eligible": len(eligible), "overdue": len(overdue),
            "upcoming": len(upcoming),
            "recorded_due_dates": sum(1 for r in eligible if r.kind == "recorded"),
            "estimated_windows": sum(1 for r in eligible if r.kind != "recorded")}


def get_crm_context(snapshot_id: str, customer_id: str) -> dict:
    c = crm(snapshot_id)
    contact = c.get("contacts", {}).get(customer_id)
    return {"synthetic": bool(c.get("synthetic")), "has_verified_contact": bool(contact and contact.get("verified")),
            "contact_name": contact.get("name") if contact else None,
            "account_owner": c.get("account_owners", {}).get(customer_id, "Unassigned"),
            "notes": c.get("notes", {}).get(customer_id, [])}


def verified_contact_email(snapshot_id: str, customer_id: str) -> str | None:
    contact = crm(snapshot_id).get("contacts", {}).get(customer_id)
    return contact["email"] if contact and contact.get("verified") else None


def known_teams(snapshot_id: str) -> set[str]:
    owners = set(crm(snapshot_id).get("account_owners", {}).values())
    return owners | {"Team North", "Team South", "Unassigned"}


def get_peer_benchmark(snapshot_id: str, customer_id: str) -> dict:
    """Median trailing-12-month volume of same-industry accounts."""
    snapshot = data.load_snapshot(snapshot_id)
    profiles = {p.customer_id: p for p in snapshot["profiles"]}
    me = profiles.get(customer_id)
    months = snapshot["month_grid"][-12:]
    totals: dict[str, int] = {}
    for row in snapshot["history"]:
        if row.month in months:
            totals[row.customer_id] = totals.get(row.customer_id, 0) + row.calibration_events
    peers = [totals.get(cid, 0) for cid, p in profiles.items()
             if cid != customer_id and me and p.industry_id == me.industry_id]
    everyone = sorted(totals.values())
    mine = totals.get(customer_id, 0)
    rank = sum(1 for v in everyone if v <= mine) / len(everyone) if everyone else None
    return {"industry": me.industry_label if me else None, "peer_count": len(peers),
            "peer_median_12m": statistics.median(peers) if peers else None,
            "customer_12m": mine, "portfolio_percentile": round(rank, 2) if rank is not None else None}
