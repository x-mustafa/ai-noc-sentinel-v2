import json
import os
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, validator
from typing import Optional, Literal

from app.config import settings
from app.deps import get_session, require_admin, require_operator
from app.database import fetch_all, fetch_one, execute
from app.services.workflow_engine import (
    approve_pending_workflow_action,
    reject_pending_workflow_action,
    reload_scheduled_workflows,
    trigger_workflow_manually,
)

router = APIRouter()

WA_SERVICE = os.getenv("WHATSAPP_SERVICE_URL", "http://localhost:3001")

_VALID_EMPLOYEES = {"aria", "nexus", "cipher", "vega"}


class WorkflowBody(BaseModel):
    name:            str = Field(..., max_length=120)
    description:     str = Field("", max_length=500)
    trigger_type:    Literal["alarm", "schedule", "threshold", "manual"] = "manual"
    trigger_config:  Optional[str] = None
    employee_id:     str = "aria"
    prompt_template: str = Field(
        "Analyze the current network state and provide a brief status report.",
        max_length=4000,
    )
    action_type:     str = "log"   # JSON array string for multi-action e.g. '["log","email"]'
    action_config:   Optional[str] = None
    risk_tier:       Literal["observe", "safe_auto", "approval_required", "forbidden"] = "safe_auto"
    is_active:       bool = True

    @validator("employee_id")
    def valid_employee(cls, v):
        if v not in _VALID_EMPLOYEES:
            raise ValueError(f"employee_id must be one of: {', '.join(sorted(_VALID_EMPLOYEES))}")
        return v


class ApprovalDecisionBody(BaseModel):
    note: Optional[str] = Field(None, max_length=1000)


@router.get("")
async def list_workflows(session: dict = Depends(get_session)):
    rows = await fetch_all("SELECT * FROM workflows ORDER BY id")
    for r in rows:
        r["is_active"] = bool(r.get("is_active"))
    return rows


@router.post("")
async def create_workflow(body: WorkflowBody, session: dict = Depends(require_operator)):
    if not body.name:
        raise HTTPException(400, "Name required")
    wf_id = await execute(
        "INSERT INTO workflows (name, description, trigger_type, trigger_config, "
        "employee_id, prompt_template, action_type, action_config, risk_tier, is_active) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (body.name, body.description, body.trigger_type,
         body.trigger_config, body.employee_id,
         body.prompt_template, body.action_type,
         body.action_config, body.risk_tier, int(body.is_active)),
    )
    await reload_scheduled_workflows()
    return {"ok": True, "id": wf_id}


@router.get("/approvals")
async def list_approvals(
    status: str = "pending",
    session: dict = Depends(require_operator),
):
    rows = await fetch_all(
        "SELECT wa.id, wa.workflow_id, wa.workflow_run_id, wa.effective_risk_tier, wa.action_plan, "
        "wa.status, wa.requested_by, wa.decision_note, wa.decided_by, wa.requested_at, wa.decided_at, "
        "w.name AS workflow_name "
        "FROM workflow_approvals wa "
        "JOIN workflows w ON w.id=wa.workflow_id "
        "WHERE wa.status=%s "
        "ORDER BY wa.requested_at DESC LIMIT 100",
        (status,),
    )
    for row in rows:
        row["requested_at"] = str(row.get("requested_at", "") or "")
        row["decided_at"] = str(row.get("decided_at", "") or "")
    return rows


@router.post("/approvals/{approval_id:int}/approve")
async def approve_workflow_approval(
    approval_id: int,
    body: ApprovalDecisionBody,
    session: dict = Depends(require_operator),
):
    try:
        return await approve_pending_workflow_action(
            approval_id,
            session.get("username", "operator"),
            body.note,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/approvals/{approval_id:int}/reject")
async def reject_workflow_approval(
    approval_id: int,
    body: ApprovalDecisionBody,
    session: dict = Depends(require_operator),
):
    try:
        return await reject_pending_workflow_action(
            approval_id,
            session.get("username", "operator"),
            body.note,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/{wf_id:int}")
async def get_workflow(wf_id: int, session: dict = Depends(get_session)):
    row = await fetch_one("SELECT * FROM workflows WHERE id=%s", (wf_id,))
    if not row:
        raise HTTPException(404, "Not found")
    row["is_active"] = bool(row.get("is_active"))
    return row


@router.put("/{wf_id:int}")
async def update_workflow(wf_id: int, body: WorkflowBody, session: dict = Depends(require_operator)):
    row = await fetch_one("SELECT id FROM workflows WHERE id=%s", (wf_id,))
    if not row:
        raise HTTPException(404, "Not found")
    await execute(
        "UPDATE workflows SET name=%s, description=%s, trigger_type=%s, trigger_config=%s, "
        "employee_id=%s, prompt_template=%s, action_type=%s, action_config=%s, risk_tier=%s, is_active=%s "
        "WHERE id=%s",
        (body.name, body.description, body.trigger_type,
         body.trigger_config, body.employee_id,
         body.prompt_template, body.action_type,
         body.action_config, body.risk_tier, int(body.is_active), wf_id),
    )
    await reload_scheduled_workflows()
    return {"ok": True}


@router.delete("/{wf_id:int}")
async def delete_workflow(wf_id: int, session: dict = Depends(require_admin)):
    await execute("DELETE FROM workflow_runs WHERE workflow_id=%s", (wf_id,))
    await execute("DELETE FROM workflows WHERE id=%s", (wf_id,))
    await reload_scheduled_workflows()
    return {"ok": True}


@router.post("/{wf_id:int}/trigger")
async def manual_trigger(wf_id: int, session: dict = Depends(require_operator)):
    row = await fetch_one("SELECT id FROM workflows WHERE id=%s", (wf_id,))
    if not row:
        raise HTTPException(404, "Workflow not found")
    result = await trigger_workflow_manually(wf_id, session.get("username", "system"))
    return result


@router.get("/{wf_id:int}/runs")
async def get_runs(wf_id: int, session: dict = Depends(get_session)):
    rows = await fetch_all(
        "SELECT id, trigger_data, ai_response, action_result, status, "
        "outcome, outcome_note, outcome_by, outcome_at, created_at "
        "FROM workflow_runs WHERE workflow_id=%s ORDER BY created_at DESC LIMIT 50",
        (wf_id,),
    )
    for r in rows:
        r["created_at"] = str(r.get("created_at", ""))
        r["outcome_at"] = str(r.get("outcome_at", "") or "")
    return rows


# ── F5 — Outcome Tracking ───────────────────────────────────────────────────────

class OutcomeBody(BaseModel):
    outcome:      Literal["correct", "incorrect", "escalated", "ignored"]
    outcome_note: Optional[str] = Field(None, max_length=1000)


@router.post("/{wf_id:int}/runs/{run_id:int}/outcome")
async def mark_run_outcome(
    wf_id:  int,
    run_id: int,
    body:   OutcomeBody,
    session: dict = Depends(require_operator),
):
    """
    Mark the outcome of a workflow run (correct / incorrect / escalated / ignored).
    Updates the employee_performance accuracy table automatically.
    """
    run = await fetch_one(
        "SELECT wr.id, wr.status, wr.outcome, w.employee_id, w.trigger_type, w.name "
        "FROM workflow_runs wr JOIN workflows w ON w.id=wr.workflow_id "
        "WHERE wr.id=%s AND wr.workflow_id=%s",
        (run_id, wf_id),
    )
    if not run:
        raise HTTPException(404, "Run not found")
    if run.get("status") not in ("success", "error"):
        raise HTTPException(400, "Can only mark completed runs")

    marked_by = session.get("username", "operator")
    await execute(
        "UPDATE workflow_runs "
        "SET outcome=%s, outcome_note=%s, outcome_by=%s, outcome_at=NOW() "
        "WHERE id=%s",
        (body.outcome, body.outcome_note, marked_by, run_id),
    )

    # Update employee_performance aggregate
    emp_id     = run.get("employee_id") or "aria"
    task_type  = run.get("trigger_type") or "manual"
    domain     = (run.get("name") or "workflow")[:100]
    is_correct = 1 if body.outcome == "correct" else 0

    await execute(
        "INSERT INTO employee_performance (employee_id, task_type, domain, correct_count, total_count) "
        "VALUES (%s,%s,%s,%s,1) "
        "ON DUPLICATE KEY UPDATE "
        "correct_count = correct_count + %s, "
        "total_count   = total_count + 1",
        (emp_id, task_type, domain, is_correct, is_correct),
    )

    return {"ok": True, "outcome": body.outcome}


_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "169.254.169.254"}


class TestWebhookBody(BaseModel):
    url: str
    payload: dict = {}


@router.post("/test-webhook")
async def test_webhook(body: TestWebhookBody, session: dict = Depends(require_operator)):
    """Send a test ping to a webhook URL (for n8n testing). Requires operator role."""
    from urllib.parse import urlparse
    import httpx
    parsed = urlparse(body.url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "Only http/https URLs allowed")
    if (parsed.hostname or "").lower() in _BLOCKED_HOSTS:
        raise HTTPException(400, "Internal/loopback addresses not allowed for webhook test")
    try:
        async with httpx.AsyncClient(verify=settings.outbound_tls_verify, timeout=10) as client:
            resp = await client.post(body.url, json=body.payload,
                                     headers={"Content-Type": "application/json",
                                              "X-Source": "NOC-Sentinel-Test"})
        return {"ok": True, "status": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── WhatsApp proxy routes (all browser→server→Node.js, so remote browsers work) ──
# WA_SERVICE is defined at module top from WHATSAPP_SERVICE_URL env var


async def _wa_get(path: str):
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{WA_SERVICE}{path}")
        if resp.status_code == 200:
            return resp.json()
        return {"error": resp.text[:200]}
    except Exception as e:
        return {"error": str(e)}


async def _wa_post(path: str, body: dict = None):
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{WA_SERVICE}{path}", json=body or {})
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


async def _wa_delete(path: str):
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(f"{WA_SERVICE}{path}")
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


@router.get("/wa/status")
async def wa_status(session: dict = Depends(get_session)):
    return await _wa_get("/status")


@router.get("/wa/log/{emp_id}")
async def wa_log(emp_id: str, session: dict = Depends(get_session)):
    return await _wa_get(f"/log/{emp_id}")


@router.get("/wa/groups/{emp_id}")
async def wa_groups(emp_id: str, session: dict = Depends(get_session)):
    return await _wa_get(f"/groups/{emp_id}")


class WaSendBody(BaseModel):
    to: str
    message: str


@router.post("/wa/send/{emp_id}")
async def wa_send(emp_id: str, body: WaSendBody, session: dict = Depends(get_session)):
    return await _wa_post(f"/send/{emp_id}", {"to": body.to, "message": body.message})


@router.post("/wa/reconnect/{emp_id}")
async def wa_reconnect(emp_id: str, session: dict = Depends(get_session)):
    return await _wa_post(f"/reconnect/{emp_id}")


@router.delete("/wa/logout/{emp_id}")
async def wa_logout(emp_id: str, session: dict = Depends(require_operator)):
    return await _wa_delete(f"/logout/{emp_id}")


# Keep old route for backward compat
@router.get("/wa-groups/{emp_id}")
async def get_wa_groups(emp_id: str, session: dict = Depends(get_session)):
    return await _wa_get(f"/groups/{emp_id}")


# ── WhatsApp conversations (DMs + groups with message history) ─────────────────

@router.get("/wa/conversations/{emp_id}")
async def wa_conversations(emp_id: str, session: dict = Depends(get_session)):
    """List all tracked conversations (DMs + groups) with last message preview."""
    return await _wa_get(f"/conversations/{emp_id}")


@router.get("/wa/conversations/{emp_id}/{jid:path}")
async def wa_conversation_messages(
    emp_id: str, jid: str, session: dict = Depends(get_session)
):
    """Get message history for a specific conversation."""
    return await _wa_get(f"/conversations/{emp_id}/{jid}")


class WaReplyBody(BaseModel):
    to:      str   # JID or phone number
    message: str


@router.post("/wa/reply/{emp_id}")
async def wa_reply(emp_id: str, body: WaReplyBody, session: dict = Depends(require_operator)):
    """Manually send a message as an employee (DM or group)."""
    return await _wa_post(f"/reply/{emp_id}", {"to": body.to, "message": body.message})
