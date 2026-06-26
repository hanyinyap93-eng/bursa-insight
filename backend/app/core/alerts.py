"""
Index Health threshold alerts — Bursa Insight.

Users register alerts on a metric crossing a threshold; evaluate() recomputes
the current values and returns which alerts are firing. In the MVP, alerts live
in an in-memory store (swap for the DB later). A scheduled job calls evaluate()
and dispatches notifications for newly-triggered alerts.

Supported metrics:
  index_health        - KLCI breadth Index Health %
  sector_health:<SEC> - a sector index's health (e.g. sector_health:TECHNOLOGY)
  component:<comp>     - a KLCI component sub-score (momentum/rsi/sma/hl)
"""
from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from typing import Optional

from . import index_health as ih
from . import service

_ids = itertools.count(1)
_lock = threading.Lock()


@dataclass
class Alert:
    metric: str                       # e.g. "index_health", "sector_health:TECHNOLOGY"
    op: str                           # "above" | "below" | "cross_above" | "cross_below"
    threshold: float
    id: int = field(default_factory=lambda: next(_ids))
    label: Optional[str] = None
    user_id: Optional[str] = None
    active: bool = True
    last_value: Optional[float] = None
    last_state: Optional[bool] = None  # was the raw above/below condition true last check
    triggered: bool = False

    def to_dict(self):
        return {
            "id": self.id, "metric": self.metric, "op": self.op,
            "threshold": self.threshold, "label": self.label,
            "active": self.active, "last_value": self.last_value,
            "triggered": self.triggered,
        }


_alerts: dict[int, Alert] = {}


def create_alert(metric: str, op: str, threshold: float,
                 label: Optional[str] = None, user_id: Optional[str] = None) -> Alert:
    if op not in ("above", "below", "cross_above", "cross_below"):
        raise ValueError(f"invalid op: {op}")
    a = Alert(metric=metric, op=op, threshold=float(threshold), label=label, user_id=user_id)
    with _lock:
        _alerts[a.id] = a
    return a


def list_alerts(user_id: Optional[str] = None) -> list[Alert]:
    with _lock:
        return [a for a in _alerts.values() if user_id is None or a.user_id == user_id]


def delete_alert(alert_id: int) -> bool:
    with _lock:
        return _alerts.pop(alert_id, None) is not None


def _current_value(metric: str) -> Optional[float]:
    """Resolve a metric name to its latest value."""
    if metric == "index_health":
        r = service.get_health("KLCI")
        warm = r.cfg.warmup
        return round(float(r.health_pct.iloc[warm:].iloc[-1]), 2)
    if metric.startswith("component:"):
        comp = metric.split(":", 1)[1]
        r = service.get_health("KLCI")
        if comp in ih.HEALTH_COMPONENTS:
            return round(float(r.component_pct[comp].iloc[-1]), 2)
        return None
    if metric.startswith("sector_health:"):
        sec = metric.split(":", 1)[1]
        df = service.get_sector_health()
        if df is not None and not df.empty and sec in df.columns:
            col = df[sec].dropna()
            return round(float(col.iloc[-1]), 2) if len(col) else None
        return None
    return None


def evaluate(user_id: Optional[str] = None) -> list[dict]:
    """Recompute metrics and return alerts that are firing now.

    For 'cross_*' ops a fire requires the condition to be newly true since the
    previous evaluate() (edge-triggered). 'above'/'below' fire whenever the
    condition holds (level-triggered).
    """
    fired = []
    for a in list_alerts(user_id):
        if not a.active:
            continue
        val = _current_value(a.metric)
        if val is None:
            continue
        above = val > a.threshold
        below = val < a.threshold
        is_fire = False
        if a.op == "above":
            is_fire = above
        elif a.op == "below":
            is_fire = below
        elif a.op == "cross_above":
            is_fire = above and (a.last_state is False)
        elif a.op == "cross_below":
            is_fire = below and (a.last_state is True)
        a.last_state = above
        a.last_value = val
        a.triggered = is_fire
        if is_fire:
            fired.append({**a.to_dict(), "value": val})
    return fired
