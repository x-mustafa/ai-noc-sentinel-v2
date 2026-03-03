"""
Incident Ownership — F2

Create, track, update, and close incidents with AI employee ownership.
Employees are assigned as owners; the system injects open incidents into
their prompts so they always know what they're responsible for.
"""
import json
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, Literal

from app.deps import get_session, require_operator
from app.database import fetch_one, fetch_all, execute
from app.services.ai_provider import resolve_runtime_ai
from app.services.employee_context import set_employee_investigating, set_employee_available

logger = logging.getLogger(__name__)
router = APIRouter()

_VALID_EMPLOYEES = {"aria", "nexus", "cipher", "vega"}

SEV_LABELS = {1: "Info", 2: "Warning", 3: "Average", 4: "High", 5: "Disaster"}


# ── Request models ─────────────────────────────────────────────────────────────

class IncidentBody(BaseModel):
    title:           str = Field(..., max_length=300)
    description:     Optional[str] = None
    owner_id:        Optional[str] = "aria"
    severity:        int = 3          # 1–5
    host:            Optional[str] = None
    zabbix_event_id: Optional[str] = None


class IncidentUpdateBody(BaseModel):
    update_text: str = Field(..., max_length=3000)
    update_type: Literal["status", "finding", "action", "escalation", "resolution"] = "finding"
    employee_id: Optional[str] = None


class IncidentPatchBody(BaseModel):
    status:   Optional[Literal["open", "investigating", "resolved", "closed"]] = None
    rca:      Optional[str] = None
    owner_id: Optional[str] = None
    title:    Optional[str] = None


# ── List / Create ──────────────────────────────────────────────────────────────

@router.get("")
async def list_incidents(
    status: Optional[str] = None,
    owner:  Optional[str] = None,
    session: dict = Depends(get_session),
):
    """List incidents. Defaults to all non-closed incidents."""
    where, params = [], []

    if status:
        where.append("status=%s")
        params.append(status)
    else:
        where.append("status NOT IN ('closed')")

    if owner:
        where.append("owner_id=%s")
        params.append(owner)

    clause = "WHERE " + " AND ".join(where) if where else ""
    rows = await fetch_all(
        f"SELECT * FROM incidents {clause} ORDER BY severity DESC, started_at DESC LIMIT 100",
        tuple(params) or None,
    )
    for r in rows:
        r["started_at"]  = str(r.get("started_at", ""))
        r["resolved_at"] = str(r.get("resolved_at", "") or "")
        r["sev_label"]   = SEV_LABELS.get(r.get("severity", 3), "Unknown")
    return rows


@router.post("")
async def create_incident(body: IncidentBody, session: dict = Depends(get_session)):
    """Create a new incident. Optionally assign an AI employee as owner."""
    if body.owner_id and body.owner_id not in _VALID_EMPLOYEES:
        raise HTTPException(
            400,
            f"Invalid owner. Must be one of: {', '.join(sorted(_VALID_EMPLOYEES))}",
        )
    if not 1 <= body.severity <= 5:
        raise HTTPException(400, "severity must be 1–5")

    inc_id = await execute(
        "INSERT INTO incidents "
        "(title, description, owner_id, severity, host, zabbix_event_id, created_by) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (
            body.title,
            body.description,
            body.owner_id,
            body.severity,
            body.host,
            body.zabbix_event_id,
            session.get("username", "system"),
        ),
    )

    # Update employee status to investigating when assigned
    if body.owner_id:
        await set_employee_investigating(
            body.owner_id,
            f"INC-{inc_id:04d}: {body.title[:80]}",
        )

    return {"ok": True, "id": inc_id}


# ── Single Incident ────────────────────────────────────────────────────────────

@router.get("/{inc_id:int}")
async def get_incident(inc_id: int, session: dict = Depends(get_session)):
    """Get a single incident with all its updates."""
    row = await fetch_one("SELECT * FROM incidents WHERE id=%s", (inc_id,))
    if not row:
        raise HTTPException(404, "Incident not found")

    updates = await fetch_all(
        "SELECT * FROM incident_updates WHERE incident_id=%s ORDER BY created_at ASC",
        (inc_id,),
    )
    for u in updates:
        u["created_at"] = str(u.get("created_at", ""))

    row["updates"]    = updates
    row["started_at"] = str(row.get("started_at", ""))
    row["resolved_at"] = str(row.get("resolved_at", "") or "")
    row["sev_label"]   = SEV_LABELS.get(row.get("severity", 3), "Unknown")
    return row


@router.put("/{inc_id:int}")
async def update_incident(
    inc_id: int,
    body: IncidentPatchBody,
    session: dict = Depends(require_operator),
):
    """Update incident status, RCA, title, or owner."""
    row = await fetch_one("SELECT * FROM incidents WHERE id=%s", (inc_id,))
    if not row:
        raise HTTPException(404, "Incident not found")

    if body.owner_id and body.owner_id not in _VALID_EMPLOYEES:
        raise HTTPException(400, "Invalid owner")

    sets, vals = [], []
    if body.status is not None:
        sets.append("status=%s")
        vals.append(body.status)
        if body.status in ("resolved", "closed"):
            sets.append("resolved_at=NOW()")
    if body.rca is not None:
        sets.append("rca=%s")
        vals.append(body.rca)
    if body.owner_id is not None:
        sets.append("owner_id=%s")
        vals.append(body.owner_id)
    if body.title is not None:
        sets.append("title=%s")
        vals.append(body.title[:300])

    if sets:
        vals.append(inc_id)
        await execute(
            "UPDATE incidents SET " + ",".join(sets) + " WHERE id=%s",
            tuple(vals),
        )

    # Free up owner when incident is resolved/closed
    if body.status in ("resolved", "closed") and row.get("owner_id"):
        await set_employee_available(row["owner_id"])

    # If owner changed, update status for new owner
    if body.owner_id and body.owner_id != row.get("owner_id"):
        if row.get("owner_id"):
            await set_employee_available(row["owner_id"])
        await set_employee_investigating(
            body.owner_id,
            f"INC-{inc_id:04d}: {row['title'][:80]}",
        )

    return {"ok": True}


# ── Updates ────────────────────────────────────────────────────────────────────

@router.put("/{inc_id:int}/link-runbook/{rb_id:int}")
async def link_runbook(
    inc_id: int,
    rb_id: int,
    session: dict = Depends(require_operator),
):
    incident = await fetch_one("SELECT id FROM incidents WHERE id=%s", (inc_id,))
    if not incident:
        raise HTTPException(404, "Incident not found")
    runbook = await fetch_one("SELECT id FROM runbooks WHERE id=%s", (rb_id,))
    if not runbook:
        raise HTTPException(404, "Runbook not found")

    await execute("UPDATE incidents SET runbook_id=%s WHERE id=%s", (rb_id, inc_id))
    await execute(
        "UPDATE runbooks SET source_incident_id=COALESCE(source_incident_id, %s) WHERE id=%s",
        (inc_id, rb_id),
    )
    return {"ok": True, "incident_id": inc_id, "runbook_id": rb_id}


@router.post("/{inc_id:int}/update")
async def add_incident_update(
    inc_id: int,
    body: IncidentUpdateBody,
    session: dict = Depends(get_session),
):
    """Add a human or AI update to an incident."""
    row = await fetch_one("SELECT id FROM incidents WHERE id=%s", (inc_id,))
    if not row:
        raise HTTPException(404, "Incident not found")

    employee_id = body.employee_id or session.get("username", "human")
    await execute(
        "INSERT INTO incident_updates (incident_id, employee_id, update_text, update_type) "
        "VALUES (%s,%s,%s,%s)",
        (inc_id, employee_id, body.update_text, body.update_type),
    )
    return {"ok": True}


# ── Assign ─────────────────────────────────────────────────────────────────────

@router.post("/{inc_id:int}/assign/{employee_id}")
async def assign_incident(
    inc_id:      int,
    employee_id: str,
    session: dict = Depends(require_operator),
):
    """Reassign an incident to a different AI employee."""
    if employee_id not in _VALID_EMPLOYEES:
        raise HTTPException(400, f"Invalid employee: {employee_id}")

    row = await fetch_one("SELECT owner_id, title FROM incidents WHERE id=%s", (inc_id,))
    if not row:
        raise HTTPException(404, "Incident not found")

    previous = row.get("owner_id")
    if previous and previous != employee_id:
        await set_employee_available(previous)

    await execute(
        "UPDATE incidents SET owner_id=%s WHERE id=%s",
        (employee_id, inc_id),
    )
    await execute(
        "INSERT INTO incident_updates (incident_id, employee_id, update_text, update_type) "
        "VALUES (%s,%s,%s,'status')",
        (
            inc_id,
            session.get("username", "system"),
            f"Incident reassigned to {employee_id.upper()}",
        ),
    )
    await set_employee_investigating(
        employee_id,
        f"INC-{inc_id:04d}: {row['title'][:80]}",
    )
    return {"ok": True}


# ── AI Status Update ───────────────────────────────────────────────────────────

@router.post("/{inc_id:int}/ask-update")
async def ask_incident_update(
    inc_id: int,
    session: dict = Depends(get_session),
):
    """
    Ask the assigned AI employee to generate a current status update for this incident.
    Runs in the background; check incident updates after a few seconds.
    """
    row = await fetch_one("SELECT * FROM incidents WHERE id=%s", (inc_id,))
    if not row:
        raise HTTPException(404, "Incident not found")
    if not row.get("owner_id"):
        raise HTTPException(400, "No owner assigned to this incident")

    updates = await fetch_all(
        "SELECT employee_id, update_text, update_type, created_at "
        "FROM incident_updates WHERE incident_id=%s ORDER BY created_at ASC",
        (inc_id,),
    )
    updates_text = "\n".join(
        f"[{u['update_type'].upper()}] {u['employee_id']}: {u['update_text']}"
        for u in updates
    ) or "No updates yet."

    prompt = (
        f"INCIDENT STATUS REQUEST: INC-{inc_id:04d}\n"
        f"Title: {row['title']}\n"
        f"Severity: {row['severity']}/5 ({SEV_LABELS.get(row['severity'], 'Unknown')})\n"
        f"Host: {row.get('host') or 'Unknown'}\n"
        f"Current Status: {row['status']}\n"
        f"Started: {row.get('started_at')}\n\n"
        f"UPDATES SO FAR:\n{updates_text}\n\n"
        f"Provide a concise current status update: "
        f"what you know, what you are actively doing, and what the next step is. "
        f"Under 150 words. Direct and actionable."
    )

    import asyncio
    asyncio.create_task(_ai_incident_update(inc_id, row["owner_id"], prompt))

    return {
        "ok": True,
        "message": f"{row['owner_id'].upper()} is generating a status update...",
    }


async def _ai_incident_update(inc_id: int, employee_id: str, prompt: str) -> None:
    """Background: run AI and save result as an incident update."""
    try:
        from app.services.employee_prompt import build_employee_system_prompt
        from app.services.ai_stream import stream_ai

        cfg = await fetch_one("SELECT * FROM zabbix_config LIMIT 1") or {}
        provider, model, api_key = resolve_runtime_ai(cfg)
        if not api_key:
            return

        persona = await build_employee_system_prompt(employee_id)
        system = (
            (persona or f"You are {employee_id.upper()}, an AI NOC employee.")
            + "\n\nINCIDENT UPDATE MODE: Generate a brief factual status update. "
            "Under 150 words. No preamble."
        )

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
                "INSERT INTO incident_updates "
                "(incident_id, employee_id, update_text, update_type) "
                "VALUES (%s,%s,%s,'finding')",
                (inc_id, employee_id, full_response.strip()[:3000]),
            )
    except Exception as e:
        logger.error(f"_ai_incident_update({inc_id}, {employee_id}) failed: {e}")


# ── F6 — Generate Runbook from Resolved Incident ───────────────────────────────

@router.post("/{inc_id:int}/generate-runbook")
async def generate_runbook_from_incident(
    inc_id: int,
    session: dict = Depends(require_operator),
):
    """
    Ask the assigned AI employee to draft a runbook from a resolved/closed incident.
    Saves the draft to the runbooks table; returns the new runbook ID.
    """
    row = await fetch_one("SELECT * FROM incidents WHERE id=%s", (inc_id,))
    if not row:
        raise HTTPException(404, "Incident not found")
    if row.get("status") not in ("resolved", "closed"):
        raise HTTPException(400, "Can only generate runbooks from resolved or closed incidents")

    updates = await fetch_all(
        "SELECT employee_id, update_text, update_type, created_at "
        "FROM incident_updates WHERE incident_id=%s ORDER BY created_at ASC",
        (inc_id,),
    )
    updates_text = "\n".join(
        f"[{u['update_type'].upper()}] {u['employee_id']}: {u['update_text']}"
        for u in updates
    ) or "No updates recorded."

    employee_id = row.get("owner_id") or "vega"

    import asyncio
    rb_id_holder: list[int] = []
    asyncio.create_task(
        _ai_generate_runbook(inc_id, row, updates_text, employee_id, rb_id_holder)
    )

    return {
        "ok": True,
        "message": f"{employee_id.upper()} is drafting a runbook from INC-{inc_id:04d}. "
                   "Check /api/runbooks shortly.",
    }


async def _ai_generate_runbook(
    inc_id: int,
    incident: dict,
    updates_text: str,
    employee_id: str,
    result_holder: list,
) -> None:
    """Background: draft a runbook from incident data and save it."""
    try:
        from app.services.employee_prompt import build_employee_system_prompt
        from app.services.ai_stream import stream_ai

        cfg = await fetch_one("SELECT * FROM zabbix_config LIMIT 1") or {}
        provider, model, api_key = resolve_runtime_ai(cfg)
        if not api_key:
            return
        persona = await build_employee_system_prompt(employee_id)

        system = (
            (persona or f"You are {employee_id.upper()}, a NOC AI employee.")
            + "\n\nRUNBOOK GENERATION MODE: You are drafting a reusable runbook from "
            "a resolved incident. Structure your output as valid JSON with these exact keys: "
            "title, trigger_desc, trigger_keywords (comma-separated), symptoms, "
            "diagnosis, resolution, prevention, rollback, estimated_mttr (integer minutes). "
            "Be concise and technical. Output only the JSON object, no markdown."
        )

        prompt = (
            f"RESOLVED INCIDENT: INC-{inc_id:04d}\n"
            f"Title: {incident.get('title', '')}\n"
            f"Host: {incident.get('host') or 'Unknown'}\n"
            f"Severity: {incident.get('severity', 3)}/5\n"
            f"RCA: {str(incident.get('rca') or 'Not recorded')[:1000]}\n\n"
            f"INCIDENT TIMELINE:\n{updates_text[:3000]}\n\n"
            f"Draft a comprehensive runbook that will help the next engineer handle "
            f"a similar issue faster."
        )

        full_response = ""
        async for chunk in stream_ai(provider, api_key, model, system, prompt):
            if "data" in chunk:
                try:
                    d = json.loads(chunk["data"])
                    full_response += d.get("t", "")
                except Exception:
                    pass

        # Parse AI JSON output
        raw = full_response.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()

        rb_data = {}
        try:
            rb_data = json.loads(raw)
        except Exception:
            # Fallback: save raw text as title + resolution
            rb_data = {
                "title":       f"Runbook: {incident.get('title', 'Unknown')[:250]}",
                "resolution":  raw[:4000],
            }

        rb_id = await execute(
            "INSERT INTO runbooks "
            "(title, author_id, source_incident_id, trigger_desc, trigger_keywords, symptoms, "
            "diagnosis, resolution, prevention, rollback, estimated_mttr, "
            "related_hosts, status, validation_status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft','candidate')",
            (
                str(rb_data.get("title", f"Runbook: INC-{inc_id:04d}"))[:300],
                employee_id,
                inc_id,
                rb_data.get("trigger_desc"),
                rb_data.get("trigger_keywords"),
                rb_data.get("symptoms"),
                rb_data.get("diagnosis"),
                rb_data.get("resolution"),
                rb_data.get("prevention"),
                rb_data.get("rollback"),
                rb_data.get("estimated_mttr"),
                incident.get("host"),
            ),
        )
        await execute("UPDATE incidents SET runbook_id=%s WHERE id=%s", (rb_id, inc_id))
        if result_holder is not None:
            result_holder.append(rb_id)

        logger.info(f"Runbook RB-{rb_id} drafted from INC-{inc_id:04d} by {employee_id}")

    except Exception as e:
        logger.error(f"_ai_generate_runbook(inc={inc_id}) failed: {e}")
