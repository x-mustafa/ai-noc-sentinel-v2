"""
Microsoft 365 router — Outlook email + Teams messaging via Graph API.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from app.deps import get_session, require_operator
from app.services.ms365 import send_email, send_teams_message, get_inbox, test_graph
from app.database import fetch_all, execute

router = APIRouter()


# ── Pydantic models ───────────────────────────────────────────────────────────

class SendEmailBody(BaseModel):
    to: str                       # comma-separated or single email
    subject: str
    body: str
    employee_id: str = "aria"
    html: bool = False


class SendTeamsBody(BaseModel):
    webhook_url: str
    message: str
    title: str = ""
    employee_id: str = "aria"


class SaveTeamsWebhookBody(BaseModel):
    name: str
    webhook_url: str
    channel: str = ""


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status")
async def m365_status(session: dict = Depends(get_session)):
    """Test Graph API connectivity and return M365 config status."""
    from app.config import settings
    result = await test_graph()
    return {
        "graph": result,
        "email": settings.ms365_email or "(not configured)",
        "configured": bool(
            settings.ms365_tenant_id and
            settings.ms365_client_id and
            settings.ms365_client_secret
        ),
    }


@router.get("/inbox")
async def inbox(limit: int = 20, session: dict = Depends(get_session)):
    """Fetch recent emails from the NOC Sentinel shared inbox."""
    messages = await get_inbox(limit=limit)
    return messages


@router.post("/send-email")
async def api_send_email(body: SendEmailBody, session: dict = Depends(require_operator)):
    """Send an email on behalf of an AI employee."""
    to_list = [t.strip() for t in body.to.split(",") if t.strip()]
    if not to_list:
        raise HTTPException(400, "No valid recipients")
    result = await send_email(
        to=to_list,
        subject=body.subject,
        body=body.body,
        employee_id=body.employee_id,
        html=body.html,
    )
    if not result["ok"]:
        raise HTTPException(500, result.get("error", "Send failed"))
    return result


@router.post("/send-teams")
async def api_send_teams(body: SendTeamsBody, session: dict = Depends(require_operator)):
    """Send a message to a Teams channel via incoming webhook."""
    result = await send_teams_message(
        webhook_url=body.webhook_url,
        message=body.message,
        title=body.title,
        employee_id=body.employee_id,
    )
    if not result["ok"]:
        raise HTTPException(500, result.get("error", "Teams send failed"))
    return result


# ── Teams webhook config (saved in DB) ────────────────────────────────────────

@router.get("/teams-webhooks")
async def list_teams_webhooks(session: dict = Depends(get_session)):
    """List saved Teams channel webhooks."""
    rows = await fetch_all("SELECT * FROM ms365_teams_webhooks ORDER BY id")
    return rows


@router.post("/teams-webhooks")
async def save_teams_webhook(body: SaveTeamsWebhookBody, session: dict = Depends(require_operator)):
    """Save a Teams incoming webhook URL."""
    await execute(
        "INSERT INTO ms365_teams_webhooks (name, webhook_url, channel) VALUES (%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE webhook_url=VALUES(webhook_url), channel=VALUES(channel)",
        (body.name, body.webhook_url, body.channel),
    )
    return {"ok": True}


@router.delete("/teams-webhooks/{webhook_id}")
async def delete_teams_webhook(webhook_id: int, session: dict = Depends(require_operator)):
    await execute("DELETE FROM ms365_teams_webhooks WHERE id=%s", (webhook_id,))
    return {"ok": True}
