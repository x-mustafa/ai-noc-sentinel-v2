"""
F13 — Change Calendar Awareness
Scheduled maintenance windows that employees factor into alarm analysis.
Active windows are injected into the employee context automatically.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.deps import get_session, require_operator
from app.database import fetch_all, fetch_one, execute

router = APIRouter()


class ChangeBody(BaseModel):
    title: str
    owner: Optional[str] = None
    employee_id: Optional[str] = None       # which AI employee is executing the change
    affected_hosts: Optional[str] = None    # JSON array: ["host1","host2"]
    expected_impact: Optional[str] = None
    start_at: str                           # ISO datetime string
    end_at: str
    notes: Optional[str] = None


class ChangeStatusUpdate(BaseModel):
    status: str                             # planned / active / completed / cancelled
    notes: Optional[str] = None


# ── CRUD ──────────────────────────────────────────────────────────────────────

@router.get("/changes")
async def list_changes(
    status: Optional[str] = None,
    session: dict = Depends(get_session),
):
    sql    = "SELECT * FROM change_calendar WHERE 1=1"
    params = []
    if status:
        sql += " AND status=%s"; params.append(status)
    else:
        sql += " AND status NOT IN ('completed','cancelled')"
    sql += " ORDER BY start_at ASC LIMIT 100"
    return await fetch_all(sql, params)


@router.get("/changes/active")
async def list_active_changes(session: dict = Depends(get_session)):
    """Returns changes currently in progress (NOW() between start_at and end_at)."""
    return await fetch_all(
        "SELECT * FROM change_calendar "
        "WHERE status IN ('planned','active') AND start_at <= NOW() AND end_at >= NOW() "
        "ORDER BY start_at ASC",
    )


@router.get("/changes/{change_id}")
async def get_change(change_id: int, session: dict = Depends(get_session)):
    row = await fetch_one("SELECT * FROM change_calendar WHERE id=%s", (change_id,))
    if not row:
        raise HTTPException(404, "Change not found")
    return row


@router.post("/changes")
async def create_change(body: ChangeBody, session: dict = Depends(require_operator)):
    change_id = await execute(
        "INSERT INTO change_calendar "
        "(title, owner, employee_id, affected_hosts, expected_impact, start_at, end_at, notes) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (body.title, body.owner, body.employee_id, body.affected_hosts,
         body.expected_impact, body.start_at, body.end_at, body.notes),
    )
    return {"id": change_id, "status": "planned"}


@router.put("/changes/{change_id}")
async def update_change(
    change_id: int, body: ChangeBody, session: dict = Depends(require_operator)
):
    existing = await fetch_one("SELECT id FROM change_calendar WHERE id=%s", (change_id,))
    if not existing:
        raise HTTPException(404, "Change not found")
    await execute(
        "UPDATE change_calendar SET title=%s, owner=%s, employee_id=%s, "
        "affected_hosts=%s, expected_impact=%s, start_at=%s, end_at=%s, notes=%s "
        "WHERE id=%s",
        (body.title, body.owner, body.employee_id, body.affected_hosts,
         body.expected_impact, body.start_at, body.end_at, body.notes, change_id),
    )
    return {"status": "updated"}


@router.patch("/changes/{change_id}/status")
async def update_change_status(
    change_id: int, body: ChangeStatusUpdate, session: dict = Depends(require_operator)
):
    valid = {"planned", "active", "completed", "cancelled"}
    if body.status not in valid:
        raise HTTPException(400, f"status must be one of {valid}")
    await execute(
        "UPDATE change_calendar SET status=%s, notes=COALESCE(%s, notes) WHERE id=%s",
        (body.status, body.notes, change_id),
    )
    return {"status": body.status}


@router.delete("/changes/{change_id}")
async def delete_change(change_id: int, session: dict = Depends(require_operator)):
    await execute(
        "UPDATE change_calendar SET status='cancelled' WHERE id=%s", (change_id,)
    )
    return {"status": "cancelled"}


# ── Context injection helper (called by employee_context.py) ──────────────────

async def get_active_change_context() -> str:
    """
    Returns a formatted string of currently active maintenance windows
    for injection into employee system prompts.
    """
    active = await fetch_all(
        "SELECT * FROM change_calendar "
        "WHERE status IN ('planned','active') AND start_at <= NOW() AND end_at >= NOW() "
        "ORDER BY start_at ASC LIMIT 10",
    )
    if not active:
        return ""

    lines = ["ACTIVE MAINTENANCE WINDOWS — factor these into your alarm analysis:"]
    for c in active:
        hosts = c.get("affected_hosts") or "unspecified hosts"
        lines.append(
            f"  • [{c['status'].upper()}] {c['title']}"
            f" | Ends: {_fmt_time(c['end_at'])}"
            f" | Hosts: {hosts}"
            f" | Impact: {c.get('expected_impact') or 'see notes'}"
        )
    lines.append(
        "Any alarms on affected hosts during these windows may be CHANGE-RELATED. "
        "Flag them as such before escalating."
    )
    return "\n".join(lines)


async def auto_activate_changes():
    """
    APScheduler job every 5 min: auto-transition planned→active and active→completed
    based on current time.
    """
    # planned → active (start_at passed, end_at in future)
    await execute(
        "UPDATE change_calendar SET status='active' "
        "WHERE status='planned' AND start_at <= NOW() AND end_at >= NOW()"
    )
    # active → completed (end_at passed)
    await execute(
        "UPDATE change_calendar SET status='completed' "
        "WHERE status='active' AND end_at < NOW()"
    )


def _fmt_time(ts) -> str:
    if not ts:
        return "N/A"
    if hasattr(ts, "strftime"):
        return ts.strftime("%Y-%m-%d %H:%M")
    return str(ts)
