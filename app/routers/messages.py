"""
F4 — Async Peer Messaging

AI employees can send tasks/questions to each other asynchronously.
The receiving employee's AI replies via background task; replies are stored
and exposed to the sender via their inbox context.
"""
import json
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from app.deps import get_session, require_operator
from app.database import fetch_one, fetch_all, execute
from app.services.ai_provider import resolve_runtime_ai

logger = logging.getLogger(__name__)
router = APIRouter()

_VALID_EMPLOYEES = {"aria", "nexus", "cipher", "vega"}


# ── Request models ──────────────────────────────────────────────────────────────

class SendMessageBody(BaseModel):
    from_employee: str
    to_employee:   str
    subject:       Optional[str] = None
    body:          str = Field(..., max_length=3000)
    context_data:  Optional[str] = None   # JSON string with extra context (e.g. incident id)
    initiated_by:  Optional[str] = None   # human username who triggered this


# ── Endpoints ───────────────────────────────────────────────────────────────────

@router.post("")
async def send_message(body: SendMessageBody, session: dict = Depends(get_session)):
    """Send a peer message from one AI employee to another. AI reply is generated in background."""
    if body.from_employee not in _VALID_EMPLOYEES:
        raise HTTPException(400, f"Invalid from_employee: {body.from_employee}")
    if body.to_employee not in _VALID_EMPLOYEES:
        raise HTTPException(400, f"Invalid to_employee: {body.to_employee}")
    if body.from_employee == body.to_employee:
        raise HTTPException(400, "Cannot send a message to yourself")

    initiated_by = body.initiated_by or session.get("username", "human")
    msg_id = await execute(
        "INSERT INTO employee_messages "
        "(from_employee, to_employee, subject, body, context_data, initiated_by) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (
            body.from_employee,
            body.to_employee,
            body.subject,
            body.body,
            body.context_data,
            initiated_by,
        ),
    )

    import asyncio
    msg = {
        "id": msg_id,
        "from_employee": body.from_employee,
        "to_employee":   body.to_employee,
        "subject":       body.subject,
        "body":          body.body,
        "context_data":  body.context_data,
    }
    asyncio.create_task(_ai_reply_to_message(msg_id, msg))

    return {"ok": True, "id": msg_id, "message": f"{body.to_employee.upper()} will reply shortly."}


@router.get("/{employee_id}/inbox")
async def get_inbox(employee_id: str, session: dict = Depends(get_session)):
    """Get pending (unread) messages for an employee."""
    if employee_id not in _VALID_EMPLOYEES:
        raise HTTPException(400, f"Invalid employee: {employee_id}")
    rows = await fetch_all(
        "SELECT id, from_employee, subject, body, status, created_at "
        "FROM employee_messages "
        "WHERE to_employee=%s AND status IN ('pending','processing') "
        "ORDER BY created_at ASC",
        (employee_id,),
    )
    for r in rows:
        r["created_at"] = str(r.get("created_at", ""))
    return rows


@router.get("/{employee_id}/all")
async def get_all_messages(
    employee_id: str,
    limit: int = 50,
    session: dict = Depends(get_session),
):
    """Get all messages (inbox + replied) for an employee — newest first."""
    if employee_id not in _VALID_EMPLOYEES:
        raise HTTPException(400, f"Invalid employee: {employee_id}")
    rows = await fetch_all(
        "SELECT id, from_employee, to_employee, subject, body, status, reply, "
        "initiated_by, created_at, replied_at "
        "FROM employee_messages "
        "WHERE to_employee=%s OR from_employee=%s "
        "ORDER BY created_at DESC LIMIT %s",
        (employee_id, employee_id, limit),
    )
    for r in rows:
        r["created_at"] = str(r.get("created_at", ""))
        r["replied_at"] = str(r.get("replied_at", "") or "")
    return rows


@router.post("/{msg_id}/process")
async def process_message(msg_id: int, session: dict = Depends(get_session)):
    """Manually trigger AI reply generation for a pending message."""
    row = await fetch_one("SELECT * FROM employee_messages WHERE id=%s", (msg_id,))
    if not row:
        raise HTTPException(404, "Message not found")
    if row["status"] not in ("pending",):
        raise HTTPException(400, f"Message is already {row['status']}")

    import asyncio
    asyncio.create_task(_ai_reply_to_message(msg_id, dict(row)))
    return {"ok": True, "message": "Reply generation started."}


@router.post("/{msg_id}/dismiss")
async def dismiss_message(msg_id: int, session: dict = Depends(require_operator)):
    """Dismiss a message without AI reply (operator only)."""
    row = await fetch_one("SELECT id, status FROM employee_messages WHERE id=%s", (msg_id,))
    if not row:
        raise HTTPException(404, "Message not found")
    if row["status"] in ("replied", "dismissed"):
        raise HTTPException(400, f"Message already {row['status']}")
    await execute(
        "UPDATE employee_messages SET status='dismissed' WHERE id=%s",
        (msg_id,),
    )
    return {"ok": True}


@router.get("/thread/incident/{incident_id}")
async def get_thread_for_incident(incident_id: int, session: dict = Depends(get_session)):
    """Get all peer messages whose context_data references a specific incident."""
    rows = await fetch_all(
        "SELECT id, from_employee, to_employee, subject, body, status, reply, "
        "created_at, replied_at "
        "FROM employee_messages "
        "WHERE context_data LIKE %s "
        "ORDER BY created_at ASC",
        (f'%"incident_id": {incident_id}%',),
    )
    for r in rows:
        r["created_at"] = str(r.get("created_at", ""))
        r["replied_at"] = str(r.get("replied_at", "") or "")
    return rows


# ── Background AI reply ─────────────────────────────────────────────────────────

async def _ai_reply_to_message(msg_id: int, msg: dict) -> None:
    """Generate an AI reply from the receiving employee and store it."""
    try:
        from app.services.employee_prompt import build_employee_system_prompt
        from app.services.employee_context import get_full_operational_context
        from app.services.ai_stream import stream_ai

        to_emp = msg.get("to_employee", "aria")

        # Mark as processing
        await execute(
            "UPDATE employee_messages SET status='processing' WHERE id=%s",
            (msg_id,),
        )

        cfg = await fetch_one("SELECT * FROM zabbix_config LIMIT 1") or {}
        provider, model, api_key = resolve_runtime_ai(cfg)
        if not api_key:
            await execute(
                "UPDATE employee_messages SET status='pending' WHERE id=%s",
                (msg_id,),
            )
            return

        persona = await build_employee_system_prompt(to_emp)
        ops_ctx = await get_full_operational_context(to_emp)

        system = (
            (persona or f"You are {to_emp.upper()}, an AI NOC employee.")
            + ops_ctx
            + "\n\nPEER MESSAGE MODE: A colleague has sent you a message requiring your "
            "expertise. Reply directly and concisely — under 300 words. "
            "No preamble or closing pleasantries."
        )

        from_emp = msg.get("from_employee", "system")
        subject  = msg.get("subject") or "(no subject)"
        body_txt = msg.get("body", "")

        prompt = (
            f"MESSAGE FROM {from_emp.upper()}:\n"
            f"Subject: {subject}\n\n"
            f"{body_txt}\n\n"
            f"Please provide your expert response."
        )

        # Include context_data if present (e.g. incident details)
        ctx_raw = msg.get("context_data")
        if ctx_raw:
            try:
                ctx_parsed = json.loads(ctx_raw)
                prompt += f"\n\nContext: {json.dumps(ctx_parsed, indent=2)}"
            except Exception:
                prompt += f"\n\nContext: {ctx_raw[:500]}"

        full_response = ""
        async for chunk in stream_ai(provider, api_key, model, system, prompt):
            if "data" in chunk:
                try:
                    d = json.loads(chunk["data"])
                    full_response += d.get("t", "")
                except Exception:
                    pass

        if full_response.strip():
            await execute(
                "UPDATE employee_messages "
                "SET status='replied', reply=%s, replied_at=NOW() "
                "WHERE id=%s",
                (full_response.strip()[:4000], msg_id),
            )
        else:
            await execute(
                "UPDATE employee_messages SET status='pending' WHERE id=%s",
                (msg_id,),
            )

    except Exception as e:
        logger.error(f"_ai_reply_to_message({msg_id}) failed: {e}")
        try:
            await execute(
                "UPDATE employee_messages SET status='pending' WHERE id=%s",
                (msg_id,),
            )
        except Exception:
            pass
