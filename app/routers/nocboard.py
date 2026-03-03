"""
F15 — NOC Board / Real-Time Situational Awareness
SSE stream + REST snapshot of the full NOC state:
  - Employee statuses
  - Open incidents
  - Active escalations
  - Recent workflow runs
  - SLA status
  - Active change windows
  - Pending peer messages
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from app.database import fetch_all, fetch_one
from app.deps import get_session

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Snapshot builder ──────────────────────────────────────────────────────────

async def build_noc_snapshot() -> dict:
    """Aggregate the full NOC state into a single dict."""

    employees, incidents, escalations, workflow_runs, sla_data, changes, messages = (
        await asyncio.gather(
            fetch_all(
                "SELECT id, title, status, current_task, status_since, employee_type "
                "FROM employee_profiles ORDER BY id"
            ),
            fetch_all(
                "SELECT id, title, severity, status, owner_id, started_at "
                "FROM incidents WHERE status IN ('open','investigating') "
                "ORDER BY severity DESC, started_at ASC LIMIT 20"
            ),
            fetch_all(
                "SELECT id, employee_id, escalated_to, channel, status, followup_at, followup_count "
                "FROM escalations WHERE status='open' ORDER BY created_at DESC LIMIT 20"
            ),
            fetch_all(
                "SELECT wr.id, w.name, w.employee_id, wr.status, wr.outcome, wr.created_at "
                "FROM workflow_runs wr JOIN workflows w ON wr.workflow_id=w.id "
                "WHERE wr.created_at >= %s ORDER BY wr.created_at DESC LIMIT 15",
                (datetime.utcnow() - timedelta(hours=24),),
            ),
            fetch_all(
                "SELECT service, target_sla, month, downtime_min FROM sla_tracker "
                "WHERE month = DATE_FORMAT(NOW(), '%%Y-%%m-01')"
            ),
            fetch_all(
                "SELECT id, title, employee_id, affected_hosts, expected_impact, start_at, end_at, status "
                "FROM change_calendar "
                "WHERE status IN ('planned','active') AND end_at >= NOW() "
                "ORDER BY start_at ASC LIMIT 10"
            ),
            fetch_all(
                "SELECT to_employee, COUNT(*) as pending "
                "FROM employee_messages WHERE status='pending' GROUP BY to_employee"
            ),
            return_exceptions=True,
        )
    )

    def _safe(v):
        return v if isinstance(v, list) else []

    employees   = _safe(employees)
    incidents   = _safe(incidents)
    escalations = _safe(escalations)
    runs        = _safe(workflow_runs)
    sla         = _safe(sla_data)
    changes     = _safe(changes)
    messages    = _safe(messages)

    # Build pending message lookup
    pending_msgs = {m["to_employee"]: m["pending"] for m in messages}

    # Enrich employee data
    emp_list = []
    for e in employees:
        emp_list.append({
            "id":           e["id"],
            "title":        e.get("title", ""),
            "status":       e.get("status", "available"),
            "current_task": e.get("current_task"),
            "status_since": _fmt(e.get("status_since")),
            "employee_type": e.get("employee_type", "noc_analyst"),
            "pending_msgs": pending_msgs.get(e["id"], 0),
            "open_incidents": sum(1 for i in incidents if i.get("owner_id") == e["id"]),
        })

    # Compute SLA health
    sla_list = []
    for s in sla:
        target     = float(s.get("target_sla", 99.99))
        downtime   = int(s.get("downtime_min", 0))
        days_month = datetime.utcnow().day
        total_min  = days_month * 24 * 60
        actual     = round(100 * (total_min - downtime) / total_min, 4) if total_min else 100
        budget_pct = round(100 * (actual - target) / (100 - target), 1) if (100 - target) else 100
        sla_list.append({
            "service":    s["service"],
            "target":     target,
            "actual":     actual,
            "downtime_min": downtime,
            "budget_remaining_pct": budget_pct,
            "health":     "ok" if actual >= target else "breached",
        })

    return {
        "ts":          datetime.utcnow().isoformat(),
        "employees":   emp_list,
        "incidents":   [_jsonify(i) for i in incidents],
        "escalations": [_jsonify(e) for e in escalations],
        "workflow_runs": [_jsonify(r) for r in runs],
        "sla":         sla_list,
        "changes":     [_jsonify(c) for c in changes],
        "summary": {
            "open_incidents":    len(incidents),
            "open_escalations":  len(escalations),
            "active_changes":    sum(1 for c in changes if c.get("status") == "active"),
            "sla_breached":      sum(1 for s in sla_list if s["health"] == "breached"),
        },
    }


# ── REST snapshot ─────────────────────────────────────────────────────────────

@router.get("/nocboard/snapshot")
async def noc_snapshot(session: dict = Depends(get_session)):
    """Single JSON snapshot of the full NOC state."""
    return await build_noc_snapshot()


# ── SSE stream ────────────────────────────────────────────────────────────────

@router.get("/nocboard/stream")
async def noc_stream(session: dict = Depends(get_session)):
    """
    Server-Sent Events stream.
    Pushes a full NOC snapshot every 10 seconds.
    Frontend subscribes once and keeps its board live.
    """
    async def _generator():
        while True:
            try:
                snapshot = await build_noc_snapshot()
                yield {"event": "snapshot", "data": json.dumps(snapshot, default=str)}
            except Exception as e:
                logger.error(f"NOC board SSE error: {e}")
                yield {"event": "error", "data": json.dumps({"error": str(e)})}
            await asyncio.sleep(10)

    return EventSourceResponse(_generator())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt(ts) -> str | None:
    if ts is None:
        return None
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)


def _jsonify(row: dict) -> dict:
    """Convert datetime objects in a DB row to ISO strings."""
    out = {}
    for k, v in row.items():
        out[k] = _fmt(v) if hasattr(v, "isoformat") else v
    return out
