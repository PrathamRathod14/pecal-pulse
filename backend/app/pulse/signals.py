"""Deterministic business-signal detection over the active snapshot.

Signals are *measured* facts from monthly calibration history — not
predictions. Value is expressed as trailing-12-month calibration volume
because the source data contains no revenue figures.
"""

from __future__ import annotations

from functools import lru_cache

from ..capabilities import data


def _series(snapshot: dict) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for row in snapshot["history"]:
        out.setdefault(row.customer_id, {})[row.month] = row.calibration_events
    return out


def measure(history: dict[str, int], months: list[str]) -> dict:
    vals = [history.get(m, 0) for m in months]
    last3 = vals[-3:]
    prior12 = vals[-15:-3]
    last12 = vals[-12:]
    base = sum(prior12) / len(prior12) if prior12 else 0.0
    recent = sum(last3) / 3 if last3 else 0.0
    change = (recent - base) / base if base > 0 else None
    active = [i for i, v in enumerate(vals) if v > 0]
    silent = (len(vals) - 1 - active[-1]) if active else None
    gaps = [b - a for a, b in zip(active, active[1:])]
    usual_gap = sorted(gaps)[len(gaps) // 2] if gaps else None
    return {
        "volume_12m": sum(last12),
        "avg_last_3m": round(recent, 1),
        "avg_prior_12m": round(base, 1),
        "change_pct": round(change * 100, 1) if change is not None else None,
        "months_silent": silent,
        "usual_gap_months": usual_gap,
        "series_last_15m": [{"month": m, "calibrations": history.get(m, 0)} for m in months[-15:]],
    }


def classify(m: dict, value_rank: float) -> list[dict]:
    signals = []
    if m["months_silent"] is not None and m["months_silent"] >= 4 and (m["usual_gap_months"] or 1) <= 2:
        signals.append({"key": "gone_silent", "severity": "critical" if value_rank >= 0.5 else "high",
                        "title": f"No calibrations for {m['months_silent']} months",
                        "fact": f"Usual gap between active months is {m['usual_gap_months'] or 1}; last {m['months_silent']} months show zero activity."})
    elif m["change_pct"] is not None and m["change_pct"] <= -40:
        signals.append({"key": "volume_drop", "severity": "critical" if value_rank >= 0.6 else "high",
                        "title": f"Volume down {abs(m['change_pct']):.0f}% vs prior 12 months",
                        "fact": f"Average {m['avg_last_3m']}/month over the last 3 months vs {m['avg_prior_12m']}/month in the 12 months before."})
    elif m["change_pct"] is not None and m["change_pct"] <= -20:
        signals.append({"key": "volume_softening", "severity": "medium",
                        "title": f"Volume down {abs(m['change_pct']):.0f}%",
                        "fact": f"Average {m['avg_last_3m']}/month over the last 3 months vs {m['avg_prior_12m']}/month before."})
    if m["change_pct"] is not None and m["change_pct"] >= 25:
        signals.append({"key": "growth", "severity": "opportunity",
                        "title": f"Volume up {m['change_pct']:.0f}%",
                        "fact": f"Average {m['avg_last_3m']}/month recently vs {m['avg_prior_12m']}/month before."})
    return signals


@lru_cache(maxsize=4)
def _scan(snapshot_id: str) -> tuple:
    snapshot = data.load_snapshot(snapshot_id)
    months = snapshot["month_grid"]
    series = _series(snapshot)
    names = {p.customer_id: p for p in snapshot["profiles"]}
    measured = {cid: measure(h, months) for cid, h in series.items()}
    volumes = sorted(m["volume_12m"] for m in measured.values()) or [0]
    rows = []
    for cid, m in measured.items():
        rank = sum(1 for v in volumes if v <= m["volume_12m"]) / len(volumes)
        prior_volume = m["avg_prior_12m"] * 12
        value_rank = sum(1 for x in measured.values() if x["avg_prior_12m"] * 12 <= prior_volume) / len(measured)
        for sig in classify(m, max(rank, value_rank)):
            p = names.get(cid)
            rows.append({
                "customer_id": cid,
                "display_name": p.display_name if p else cid,
                "industry": p.industry_label if p else None,
                "value_tier": "key account" if value_rank >= 0.75 else ("mid" if value_rank >= 0.4 else "small"),
                "value_percentile": round(value_rank, 2),
                **{k: v for k, v in m.items() if k != "series_last_15m"},
                **sig,
            })
    order = {"critical": 0, "high": 1, "medium": 2, "opportunity": 3}
    rows.sort(key=lambda r: (order[r["severity"]], -r["value_percentile"]))
    return tuple(rows)


def scan(snapshot_id: str, limit: int = 50) -> list[dict]:
    return [dict(r) for r in _scan(snapshot_id)[:limit]]


def for_customer(snapshot_id: str, customer_id: str) -> dict:
    snapshot = data.load_snapshot(snapshot_id)
    history = {r.month: r.calibration_events for r in data.get_customer_detail(snapshot_id, customer_id)["history"]}
    m = measure(history, snapshot["month_grid"])
    sigs = [r for r in _scan(snapshot_id) if r["customer_id"] == customer_id]
    return {**m, "signals": [{k: s[k] for k in ("key", "severity", "title", "fact")} for s in sigs],
            "value_tier": sigs[0]["value_tier"] if sigs else None,
            "value_percentile": sigs[0]["value_percentile"] if sigs else None}
