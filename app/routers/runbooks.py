"""
F6 — Living Runbook System

AI employees maintain a library of runbooks (SOP documents) that are
automatically injected into workflow prompts when alarm keywords match.
Runbooks can be drafted by AI from resolved incidents and promoted to
'approved' status by operators.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, Literal

from app.deps import get_session, require_operator
from app.database import fetch_one, fetch_all, execute

logger = logging.getLogger(__name__)
router = APIRouter()

_VALID_EMPLOYEES = {"aria", "nexus", "cipher", "vega"}


# ── Request models ──────────────────────────────────────────────────────────────

class RunbookBody(BaseModel):
    title:             str = Field(..., max_length=300)
    author_id:         Optional[str] = None
    source_incident_id: Optional[int] = None
    trigger_desc:      Optional[str] = None
    trigger_keywords:  Optional[str] = None   # comma-separated
    symptoms:          Optional[str] = None
    diagnosis:         Optional[str] = None
    resolution:        Optional[str] = None
    prevention:        Optional[str] = None
    rollback:          Optional[str] = None
    estimated_mttr:    Optional[int] = None   # minutes
    related_hosts:     Optional[str] = None   # comma-separated
    status:            Literal["draft", "approved", "deprecated"] = "draft"
    validation_status: Literal["untested", "candidate", "validated"] = "untested"


class RunbookPatchBody(BaseModel):
    title:             Optional[str] = Field(None, max_length=300)
    trigger_desc:      Optional[str] = None
    trigger_keywords:  Optional[str] = None
    symptoms:          Optional[str] = None
    diagnosis:         Optional[str] = None
    resolution:        Optional[str] = None
    prevention:        Optional[str] = None
    rollback:          Optional[str] = None
    estimated_mttr:    Optional[int] = None
    related_hosts:     Optional[str] = None
    status:            Optional[Literal["draft", "approved", "deprecated"]] = None
    validation_status: Optional[Literal["untested", "candidate", "validated"]] = None
    last_tested:       Optional[str] = None   # YYYY-MM-DD


class MatchBody(BaseModel):
    text: str = Field(..., max_length=1000)   # alarm name or problem description


# ── List / Create ───────────────────────────────────────────────────────────────

@router.get("")
async def list_runbooks(
    status: Optional[str] = None,
    author: Optional[str] = None,
    include_deprecated: bool = False,
    session: dict = Depends(get_session),
):
    """List runbooks. Defaults to non-deprecated."""
    where, params = [], []
    if status:
        where.append("status=%s")
        params.append(status)
    elif not include_deprecated:
        where.append("status != 'deprecated'")
    if author:
        where.append("author_id=%s")
        params.append(author)

    clause = "WHERE " + " AND ".join(where) if where else ""
    rows = await fetch_all(
        f"SELECT id, title, author_id, source_incident_id, trigger_keywords, estimated_mttr, "
        f"status, validation_status, related_hosts, last_tested, updated_at "
        f"FROM runbooks {clause} ORDER BY updated_at DESC LIMIT 100",
        tuple(params) or None,
    )
    for r in rows:
        r["updated_at"] = str(r.get("updated_at", ""))
        r["last_tested"] = str(r.get("last_tested", "") or "")
    return rows


@router.post("")
async def create_runbook(body: RunbookBody, session: dict = Depends(require_operator)):
    """Create a new runbook."""
    if body.author_id and body.author_id not in _VALID_EMPLOYEES:
        raise HTTPException(400, f"Invalid author_id: {body.author_id}")

    rb_id = await execute(
        "INSERT INTO runbooks "
        "(title, author_id, source_incident_id, trigger_desc, trigger_keywords, symptoms, diagnosis, "
        "resolution, prevention, rollback, estimated_mttr, related_hosts, status, validation_status) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            body.title,
            body.author_id,
            body.source_incident_id,
            body.trigger_desc,
            body.trigger_keywords,
            body.symptoms,
            body.diagnosis,
            body.resolution,
            body.prevention,
            body.rollback,
            body.estimated_mttr,
            body.related_hosts,
            body.status,
            body.validation_status,
        ),
    )
    if body.source_incident_id:
        await execute("UPDATE incidents SET runbook_id=%s WHERE id=%s", (rb_id, body.source_incident_id))
    return {"ok": True, "id": rb_id}


# ── Single Runbook ──────────────────────────────────────────────────────────────

def _build_runbook_draft_from_incident(incident: dict, updates: list[dict]) -> dict:
    status_lines = []
    finding_lines = []
    action_lines = []
    resolution_lines = []
    for update in updates:
        line = str(update.get("update_text") or "").strip()
        if not line:
            continue
        update_type = update.get("update_type") or "finding"
        if update_type == "status":
            status_lines.append(line)
        elif update_type == "finding":
            finding_lines.append(line)
        elif update_type == "action":
            action_lines.append(line)
        elif update_type == "resolution":
            resolution_lines.append(line)

    title = f"Runbook: {incident.get('title') or 'Untitled incident'}"
    host = (incident.get("host") or "").strip()
    keywords = [host] if host else []
    for token in (incident.get("title") or "").replace("/", " ").replace("-", " ").split():
        normalized = token.strip().lower()
        if len(normalized) >= 4 and normalized not in keywords:
            keywords.append(normalized)
        if len(keywords) >= 8:
            break

    return {
        "title": title[:300],
        "trigger_desc": incident.get("description") or incident.get("title") or "",
        "trigger_keywords": ",".join(keywords[:8]),
        "symptoms": "\n".join(status_lines[:5]) or incident.get("description") or "",
        "diagnosis": "\n".join(finding_lines[:8]) or incident.get("rca") or "",
        "resolution": "\n".join((resolution_lines or action_lines)[:10]) or "Document the confirmed fix here.",
        "prevention": incident.get("rca") or "Add monitoring and operator checks to prevent recurrence.",
        "rollback": "Document rollback and validation steps before approving for production use.",
        "related_hosts": host,
    }


@router.get("/coverage")
async def runbook_coverage(session: dict = Depends(get_session)):
    totals = await fetch_one(
        "SELECT "
        "SUM(CASE WHEN status IN ('resolved','closed') THEN 1 ELSE 0 END) AS resolved_total, "
        "SUM(CASE WHEN status IN ('resolved','closed') AND runbook_id IS NOT NULL THEN 1 ELSE 0 END) AS linked_total "
        "FROM incidents"
    ) or {}
    runbook_totals = await fetch_one(
        "SELECT "
        "COUNT(*) AS total_runbooks, "
        "SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) AS approved_runbooks, "
        "SUM(CASE WHEN validation_status='validated' THEN 1 ELSE 0 END) AS validated_runbooks, "
        "SUM(CASE WHEN validation_status='candidate' THEN 1 ELSE 0 END) AS candidate_runbooks "
        "FROM runbooks"
    ) or {}
    gaps = await fetch_all(
        "SELECT id, title, severity, status, host, resolved_at "
        "FROM incidents "
        "WHERE status IN ('resolved','closed') AND runbook_id IS NULL "
        "ORDER BY resolved_at DESC LIMIT 20"
    )
    for gap in gaps:
        gap["resolved_at"] = str(gap.get("resolved_at", "") or "")
    resolved_total = int(totals.get("resolved_total") or 0)
    linked_total = int(totals.get("linked_total") or 0)
    coverage_pct = round((linked_total / resolved_total) * 100, 2) if resolved_total else 0.0
    return {
        "resolved_incidents": resolved_total,
        "linked_incidents": linked_total,
        "coverage_percent": coverage_pct,
        "runbooks_total": int(runbook_totals.get("total_runbooks") or 0),
        "approved_runbooks": int(runbook_totals.get("approved_runbooks") or 0),
        "validated_runbooks": int(runbook_totals.get("validated_runbooks") or 0),
        "candidate_runbooks": int(runbook_totals.get("candidate_runbooks") or 0),
        "gaps": gaps,
    }


@router.post("/from-incident/{inc_id:int}")
async def create_runbook_from_incident(
    inc_id: int,
    session: dict = Depends(require_operator),
):
    incident = await fetch_one("SELECT * FROM incidents WHERE id=%s", (inc_id,))
    if not incident:
        raise HTTPException(404, "Incident not found")
    if incident.get("status") not in ("resolved", "closed"):
        raise HTTPException(400, "Incident must be resolved or closed before drafting a runbook")
    if incident.get("runbook_id"):
        raise HTTPException(400, "Incident already linked to a runbook")

    updates = await fetch_all(
        "SELECT update_text, update_type FROM incident_updates WHERE incident_id=%s ORDER BY created_at ASC",
        (inc_id,),
    )
    draft = _build_runbook_draft_from_incident(incident, updates)
    rb_id = await execute(
        "INSERT INTO runbooks "
        "(title, author_id, source_incident_id, trigger_desc, trigger_keywords, symptoms, diagnosis, "
        "resolution, prevention, rollback, estimated_mttr, related_hosts, status, validation_status) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft','candidate')",
        (
            draft["title"],
            incident.get("owner_id"),
            inc_id,
            draft["trigger_desc"],
            draft["trigger_keywords"],
            draft["symptoms"],
            draft["diagnosis"],
            draft["resolution"],
            draft["prevention"],
            draft["rollback"],
            None,
            draft["related_hosts"],
        ),
    )
    await execute("UPDATE incidents SET runbook_id=%s WHERE id=%s", (rb_id, inc_id))
    return {"ok": True, "id": rb_id, "source_incident_id": inc_id}


@router.get("/{rb_id:int}")
async def get_runbook(rb_id: int, session: dict = Depends(get_session)):
    """Get full runbook content."""
    row = await fetch_one("SELECT * FROM runbooks WHERE id=%s", (rb_id,))
    if not row:
        raise HTTPException(404, "Runbook not found")
    row["updated_at"] = str(row.get("updated_at", ""))
    row["created_at"] = str(row.get("created_at", ""))
    row["last_tested"] = str(row.get("last_tested", "") or "")
    return row


@router.patch("/{rb_id:int}")
async def update_runbook(
    rb_id: int,
    body: RunbookPatchBody,
    session: dict = Depends(require_operator),
):
    """Update runbook fields."""
    row = await fetch_one("SELECT id FROM runbooks WHERE id=%s", (rb_id,))
    if not row:
        raise HTTPException(404, "Runbook not found")

    field_map = {
        "title":            body.title,
        "trigger_desc":     body.trigger_desc,
        "trigger_keywords": body.trigger_keywords,
        "symptoms":         body.symptoms,
        "diagnosis":        body.diagnosis,
        "resolution":       body.resolution,
        "prevention":       body.prevention,
        "rollback":         body.rollback,
        "estimated_mttr":   body.estimated_mttr,
        "related_hosts":    body.related_hosts,
        "status":           body.status,
        "validation_status": body.validation_status,
        "last_tested":      body.last_tested,
    }
    sets, vals = [], []
    for col, val in field_map.items():
        if val is not None:
            sets.append(f"{col}=%s")
            vals.append(val)

    if sets:
        vals.append(rb_id)
        await execute(
            "UPDATE runbooks SET " + ",".join(sets) + " WHERE id=%s",
            tuple(vals),
        )
    return {"ok": True}


@router.put("/{rb_id:int}/approve")
async def approve_runbook(rb_id: int, session: dict = Depends(require_operator)):
    """Promote a draft runbook to approved status (operator only)."""
    row = await fetch_one("SELECT id, status FROM runbooks WHERE id=%s", (rb_id,))
    if not row:
        raise HTTPException(404, "Runbook not found")
    await execute(
        "UPDATE runbooks SET status='approved' WHERE id=%s",
        (rb_id,),
    )
    return {"ok": True}


@router.delete("/{rb_id:int}")
async def deprecate_runbook(rb_id: int, session: dict = Depends(require_operator)):
    """Mark a runbook as deprecated (soft delete)."""
    row = await fetch_one("SELECT id FROM runbooks WHERE id=%s", (rb_id,))
    if not row:
        raise HTTPException(404, "Runbook not found")
    await execute(
        "UPDATE runbooks SET status='deprecated' WHERE id=%s",
        (rb_id,),
    )
    return {"ok": True}


# ── Keyword Matching ────────────────────────────────────────────────────────────

@router.post("/match")
async def match_runbooks(body: MatchBody, session: dict = Depends(get_session)):
    """
    Find approved runbooks whose trigger_keywords match the given text.
    Returns top 3 matches with a relevance score.
    """
    matches = await find_matching_runbooks(body.text)
    return matches


async def find_matching_runbooks(alarm_text: str, limit: int = 3) -> list[dict]:
    """
    Score approved runbooks by keyword overlap with alarm_text.
    Returns at most `limit` runbooks above score 0, sorted by score desc.
    Used by the workflow engine for automatic injection.
    """
    rows = await fetch_all(
        "SELECT id, title, trigger_keywords, symptoms, diagnosis, resolution, "
        "estimated_mttr FROM runbooks WHERE status='approved'",
    )
    if not rows:
        return []

    alarm_lower = alarm_text.lower()
    scored = []
    for r in rows:
        kw_str = (r.get("trigger_keywords") or "").lower()
        keywords = [k.strip() for k in kw_str.split(",") if k.strip()]
        if not keywords:
            continue
        score = sum(1 for kw in keywords if kw in alarm_lower)
        if score > 0:
            scored.append({"score": score, **r})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]


def format_runbook_for_prompt(rb: dict) -> str:
    """Format a runbook as a condensed block for system prompt injection."""
    lines = [f"\n---- RUNBOOK: {rb.get('title', 'Untitled')} ----"]
    if rb.get("symptoms"):
        lines.append(f"  Symptoms: {str(rb['symptoms'])[:300]}")
    if rb.get("diagnosis"):
        lines.append(f"  Diagnosis: {str(rb['diagnosis'])[:400]}")
    if rb.get("resolution"):
        lines.append(f"  Resolution: {str(rb['resolution'])[:500]}")
    if rb.get("rollback"):
        lines.append(f"  Rollback: {str(rb['rollback'])[:200]}")
    if rb.get("estimated_mttr"):
        lines.append(f"  Est. MTTR: {rb['estimated_mttr']} min")
    lines.append("---- END RUNBOOK ----")
    return "\n".join(lines)
