"""
F10 — Escalation Ownership
Employees own escalations and follow up automatically if no response received.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.deps import get_session, require_operator
from app.database import fetch_all, fetch_one, execute
from app.services.ai_stream import extract_text_chunk
from app.services.ai_provider import resolve_runtime_ai

router = APIRouter()


class EscalationBody(BaseModel):
    incident_id: Optional[int] = None
    employee_id: str
    escalated_to: str                      # 'management', 'ISP-vendor', 'Cisco-TAC', etc.
    channel: str = "teams"                 # teams / email / whatsapp / phone
    message_sent: Optional[str] = None
    followup_minutes: int = 30             # first follow-up after N minutes
    max_followups: int = 3


class EscalationUpdate(BaseModel):
    status: Optional[str] = None           # responded / closed
    response_note: Optional[str] = None
    followup_minutes: Optional[int] = None


# ── CRUD ──────────────────────────────────────────────────────────────────────

@router.get("/escalations")
async def list_escalations(
    status: Optional[str] = None,
    employee_id: Optional[str] = None,
    session: dict = Depends(get_session),
):
    sql    = "SELECT * FROM escalations WHERE 1=1"
    params = []
    if status:
        sql += " AND status=%s"; params.append(status)
    if employee_id:
        sql += " AND employee_id=%s"; params.append(employee_id)
    sql += " ORDER BY created_at DESC LIMIT 100"
    return await fetch_all(sql, params)


@router.get("/escalations/{esc_id}")
async def get_escalation(esc_id: int, session: dict = Depends(get_session)):
    row = await fetch_one("SELECT * FROM escalations WHERE id=%s", (esc_id,))
    if not row:
        raise HTTPException(404, "Escalation not found")
    return row


@router.post("/escalations")
async def create_escalation(body: EscalationBody, session: dict = Depends(require_operator)):
    from datetime import datetime, timedelta
    followup_at = datetime.utcnow() + timedelta(minutes=body.followup_minutes)
    esc_id = await execute(
        "INSERT INTO escalations "
        "(incident_id, employee_id, escalated_to, channel, message_sent, "
        " followup_at, max_followups) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (body.incident_id, body.employee_id, body.escalated_to,
         body.channel, body.message_sent, followup_at, body.max_followups),
    )
    return {"id": esc_id, "followup_at": followup_at.isoformat(), "status": "open"}


@router.put("/escalations/{esc_id}")
async def update_escalation(
    esc_id: int, body: EscalationUpdate, session: dict = Depends(require_operator)
):
    esc = await fetch_one("SELECT * FROM escalations WHERE id=%s", (esc_id,))
    if not esc:
        raise HTTPException(404, "Escalation not found")

    updates, params = [], []
    if body.status:
        updates.append("status=%s"); params.append(body.status)
    if body.response_note:
        updates.append("response_note=%s"); params.append(body.response_note)
    if body.followup_minutes:
        from datetime import datetime, timedelta
        new_at = datetime.utcnow() + timedelta(minutes=body.followup_minutes)
        updates.append("followup_at=%s"); params.append(new_at)
    if not updates:
        return {"status": "no changes"}
    params.append(esc_id)
    await execute(f"UPDATE escalations SET {', '.join(updates)} WHERE id=%s", params)
    return {"status": "updated"}


@router.delete("/escalations/{esc_id}")
async def close_escalation(esc_id: int, session: dict = Depends(require_operator)):
    await execute("UPDATE escalations SET status='closed' WHERE id=%s", (esc_id,))
    return {"status": "closed"}


@router.get("/escalations/incident/{incident_id}")
async def get_incident_escalations(incident_id: int, session: dict = Depends(get_session)):
    return await fetch_all(
        "SELECT * FROM escalations WHERE incident_id=%s ORDER BY created_at DESC",
        (incident_id,),
    )


# ── Background follow-up timer ─────────────────────────────────────────────────

async def run_escalation_followup():
    """
    APScheduler job: every 5 minutes, check for overdue escalations.
    When an escalation passes its followup_at time and is still open,
    the owning employee sends an automatic follow-up message.
    """
    import logging
    log = logging.getLogger(__name__)

    overdue = await fetch_all(
        "SELECT * FROM escalations "
        "WHERE status='open' AND followup_at <= NOW() AND followup_count < max_followups",
    )
    if not overdue:
        return

    log.info(f"[ESCALATIONS] {len(overdue)} overdue escalation(s) to follow up")

    for esc in overdue:
        try:
            await _send_followup(esc)
        except Exception as e:
            log.error(f"[ESCALATIONS] follow-up error for esc {esc['id']}: {e}")


async def _send_followup(esc: dict):
    """Generate a follow-up message via the owning employee's AI and save it."""
    import logging
    from datetime import datetime, timedelta
    from app.database import fetch_one as db_fetch
    from app.services.ai_stream import stream_ai
    from app.services.employee_prompt import build_employee_system_prompt

    log     = logging.getLogger(__name__)
    emp_id  = esc["employee_id"]
    count   = esc["followup_count"] + 1
    max_f   = esc["max_followups"]

    cfg = await db_fetch("SELECT * FROM zabbix_config LIMIT 1")
    if not cfg:
        return
    provider, model, api_key = resolve_runtime_ai(cfg)
    if not api_key:
        return

    # Build follow-up prompt
    incident_ctx = ""
    if esc.get("incident_id"):
        inc = await db_fetch("SELECT title FROM incidents WHERE id=%s", (esc["incident_id"],))
        if inc:
            incident_ctx = f"Related incident: [{esc['incident_id']}] {inc['title']}\n"

    prompt = (
        f"You escalated to '{esc['escalated_to']}' via {esc['channel']} "
        f"{_elapsed(esc['created_at'])} ago. No response yet.\n"
        f"{incident_ctx}"
        f"Original message: {esc.get('message_sent','(not recorded)')[:400]}\n\n"
        f"This is follow-up #{count} of {max_f}. "
        f"Write a brief, professional follow-up message. "
        f"{'Be more urgent — this is the final follow-up before escalating higher.' if count == max_f else 'Politely check on status.'}"
    )

    sys_prompt = await build_employee_system_prompt(emp_id)
    chunks = []
    async for chunk in stream_ai(
        provider, api_key, model,
        sys_prompt, prompt,
    ):
        text = extract_text_chunk(chunk)
        if text:
            chunks.append(text)
    follow_up_msg = "".join(chunks).strip()

    # Schedule next follow-up (exponential: 30m → 60m → 120m)
    next_minutes  = 30 * (2 ** count)
    next_followup = datetime.utcnow() + timedelta(minutes=next_minutes)

    if count >= max_f:
        # Max follow-ups reached — close escalation
        await execute(
            "UPDATE escalations SET followup_count=%s, status='closed' WHERE id=%s",
            (count, esc["id"]),
        )
        log.warning(
            f"[ESCALATIONS] ESC-{esc['id']}: max follow-ups reached → closed"
        )
    else:
        await execute(
            "UPDATE escalations SET followup_count=%s, followup_at=%s WHERE id=%s",
            (count, next_followup, esc["id"]),
        )

    # Save follow-up as peer message so humans can see it
    from app.database import execute as db_exec
    await db_exec(
        "INSERT INTO employee_messages (from_employee, to_employee, subject, body, initiated_by) "
        "VALUES (%s,'aria',%s,%s,'auto-escalation-followup')",
        (
            emp_id,
            f"[FOLLOWUP #{count}] Escalation to {esc['escalated_to']}"[:300],
            follow_up_msg[:3000],
        ),
    )
    log.info(
        f"[ESCALATIONS] ESC-{esc['id']}: follow-up #{count} sent for {emp_id.upper()} → {esc['escalated_to']}"
    )


def _elapsed(ts) -> str:
    """Return human-readable elapsed time since a timestamp."""
    from datetime import datetime
    if not ts:
        return "unknown time"
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    delta = datetime.utcnow() - ts
    m = int(delta.total_seconds() // 60)
    if m < 60:
        return f"{m} minutes"
    return f"{m // 60}h {m % 60}m"
