"""
Microsoft 365 router — Outlook email + Teams messaging via Graph API.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from app.deps import get_session, require_operator
from app.services.ms365 import (
    send_email,
    send_teams_message,
    get_inbox,
    get_email_body,
    mark_as_read,
    reply_to_email,
    test_graph,
    list_teams,
    list_team_channels,
    list_chats,
    get_chat_messages,
    send_to_chat,
    send_to_channel,
    get_channel_messages,
)
from app.database import fetch_all, fetch_one, execute

router = APIRouter()


# ── Pydantic models ────────────────────────────────────────────────────────────

class SendEmailBody(BaseModel):
    to:          str                  # comma-separated or single address
    subject:     str
    body:        str
    employee_id: str  = "aria"
    html:        bool = False
    cc:          Optional[str] = None # comma-separated


class ReplyEmailBody(BaseModel):
    body:        str
    employee_id: str = "aria"


class SendTeamsBody(BaseModel):
    webhook_url: str
    message:     str
    title:       str = ""
    employee_id: str = "aria"


class SaveTeamsWebhookBody(BaseModel):
    name:        str
    webhook_url: str
    channel:     str = ""


class SendChatBody(BaseModel):
    message:     str
    employee_id: str = "aria"


class SendChannelBody(BaseModel):
    message:     str
    title:       str = ""
    employee_id: str = "aria"


# ── Status / Test ──────────────────────────────────────────────────────────────

@router.get("/status")
async def m365_status(session: dict = Depends(get_session)):
    """Test Graph API connectivity and return configuration status."""
    from app.config import settings
    result = await test_graph()
    return {
        "graph":      result,
        "ok":         result.get("ok", False),
        "email":      settings.ms365_email or "(not configured)",
        "configured": result.get("ok", False),
        # Surface the fix guide directly so the frontend can display it
        "error":      result.get("error", "") if not result.get("ok") else "",
        "hint":       result.get("hint", ""),
        "setup_guide": (
            "Required Azure setup:\n"
            "  1. Azure Portal → App Registrations → [app] → API Permissions\n"
            "  2. Add application permissions: Mail.Send + Mail.ReadWrite\n"
            "  3. Grant admin consent (Global Admin required)\n"
            "  4. Set MS365_TENANT_ID, MS365_CLIENT_ID, MS365_CLIENT_SECRET, MS365_EMAIL in .env"
        ) if not result.get("ok") else "",
    }


# ── Email ──────────────────────────────────────────────────────────────────────

@router.get("/inbox")
async def inbox(
    limit: int = 20,
    session: dict = Depends(get_session),
):
    """Fetch recent emails from the NOC Sentinel shared inbox."""
    messages = await get_inbox(limit=limit)

    # Surface configuration/API errors as HTTP 503 instead of silent 200
    if messages and isinstance(messages[0], dict) and "error" in messages[0]:
        raise HTTPException(503, messages[0]["error"])

    return messages


@router.get("/inbox/{message_id}")
async def read_email(
    message_id: str,
    session: dict = Depends(get_session),
):
    """Read the full body of a single email."""
    result = await get_email_body(message_id)
    if not result.get("ok"):
        raise HTTPException(503, result.get("error", "Failed to fetch email"))
    return result


@router.patch("/inbox/{message_id}/read")
async def mark_email_read(
    message_id: str,
    session: dict = Depends(get_session),
):
    """Mark an email as read."""
    result = await mark_as_read(message_id)
    if not result.get("ok"):
        raise HTTPException(503, result.get("error", "Failed to mark as read"))
    return result


@router.post("/inbox/{message_id}/reply")
async def reply_email(
    message_id: str,
    body: ReplyEmailBody,
    session: dict = Depends(require_operator),
):
    """Reply to an email thread on behalf of an AI employee."""
    result = await reply_to_email(
        message_id=message_id,
        reply_body=body.body,
        employee_id=body.employee_id,
    )
    if not result.get("ok"):
        raise HTTPException(503, result.get("error", "Reply failed"))
    return result


@router.post("/send-email")
async def api_send_email(
    body: SendEmailBody,
    session: dict = Depends(require_operator),
):
    """Send an email on behalf of an AI employee."""
    to_list = [t.strip() for t in body.to.split(",") if t.strip()]
    if not to_list:
        raise HTTPException(400, "No valid recipients")

    cc_list = None
    if body.cc:
        cc_list = [c.strip() for c in body.cc.split(",") if c.strip()] or None

    result = await send_email(
        to=to_list,
        subject=body.subject,
        body=body.body,
        employee_id=body.employee_id,
        html=body.html,
        cc=cc_list,
    )
    if not result["ok"]:
        raise HTTPException(503, result.get("error", "Send failed"))
    return result


# ── Teams Graph API — teams, channels, chats ───────────────────────────────────

@router.get("/teams")
async def api_list_teams(session: dict = Depends(get_session)):
    """List all Teams the app has access to. Requires Team.ReadBasic.All."""
    return await list_teams()


@router.get("/teams/{team_id}/channels")
async def api_list_channels(team_id: str, session: dict = Depends(get_session)):
    """List channels in a team. Requires Channel.ReadBasic.All."""
    return await list_team_channels(team_id)


@router.get("/teams/{team_id}/channels/{channel_id}/messages")
async def api_get_channel_messages(
    team_id: str, channel_id: str, limit: int = 20,
    session: dict = Depends(get_session),
):
    """Get recent channel messages. Requires ChannelMessage.Read.All."""
    return await get_channel_messages(team_id, channel_id, limit)


@router.post("/teams/{team_id}/channels/{channel_id}/send")
async def api_send_to_channel(
    team_id: str, channel_id: str,
    body: SendChannelBody,
    session: dict = Depends(require_operator),
):
    """Send a message to a Teams channel via Graph API. Requires ChannelMessage.Send."""
    result = await send_to_channel(
        team_id=team_id, channel_id=channel_id,
        message=body.message, title=body.title, employee_id=body.employee_id,
    )
    if not result["ok"]:
        raise HTTPException(503, result.get("error", "Send failed"))
    return result


@router.get("/chats")
async def api_list_chats(limit: int = 50, session: dict = Depends(get_session)):
    """List group chats the shared mailbox is a member of. Requires Chat.ReadWrite.All."""
    return await list_chats(limit)


@router.get("/chats/{chat_id}/messages")
async def api_get_chat_messages(
    chat_id: str, limit: int = 20,
    session: dict = Depends(get_session),
):
    """Get messages from a Teams chat. Requires Chat.ReadWrite.All."""
    return await get_chat_messages(chat_id, limit)


@router.post("/chats/{chat_id}/send")
async def api_send_to_chat(
    chat_id: str,
    body: SendChatBody,
    session: dict = Depends(require_operator),
):
    """Send a message to a Teams group chat via Graph API. Requires Chat.ReadWrite.All."""
    result = await send_to_chat(
        chat_id=chat_id, message=body.message, employee_id=body.employee_id,
    )
    if not result["ok"]:
        raise HTTPException(503, result.get("error", "Send failed"))
    return result


# ── Teams ──────────────────────────────────────────────────────────────────────

@router.post("/send-teams")
async def api_send_teams(
    body: SendTeamsBody,
    session: dict = Depends(require_operator),
):
    """Send a message to a Teams channel via incoming webhook."""
    result = await send_teams_message(
        webhook_url=body.webhook_url,
        message=body.message,
        title=body.title,
        employee_id=body.employee_id,
    )
    if not result["ok"]:
        raise HTTPException(503, result.get("error", "Teams send failed"))
    return result


# ── Teams webhook config (stored in DB) ───────────────────────────────────────

@router.get("/teams-webhooks")
async def list_teams_webhooks(session: dict = Depends(get_session)):
    """List saved Teams channel webhooks."""
    rows = await fetch_all("SELECT * FROM ms365_teams_webhooks ORDER BY id")
    return rows


@router.post("/teams-webhooks")
async def save_teams_webhook(
    body: SaveTeamsWebhookBody,
    session: dict = Depends(require_operator),
):
    """Save (or update) a Teams incoming webhook URL."""
    await execute(
        "INSERT INTO ms365_teams_webhooks (name, webhook_url, channel) VALUES (%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE webhook_url=VALUES(webhook_url), channel=VALUES(channel)",
        (body.name, body.webhook_url, body.channel),
    )
    return {"ok": True}


@router.post("/teams-webhooks/{webhook_id}/test")
async def test_teams_webhook(
    webhook_id: int,
    session: dict = Depends(require_operator),
):
    """Send a test message to a saved Teams webhook."""
    row = await fetch_one("SELECT * FROM ms365_teams_webhooks WHERE id=%s", (webhook_id,))
    if not row:
        raise HTTPException(404, "Webhook not found")
    result = await send_teams_message(
        webhook_url=row["webhook_url"],
        message="NOC Sentinel connectivity test — this channel is configured correctly.",
        title="Test Message",
    )
    if not result["ok"]:
        raise HTTPException(503, result.get("error", "Test failed"))
    return result


@router.delete("/teams-webhooks/{webhook_id}")
async def delete_teams_webhook(
    webhook_id: int,
    session: dict = Depends(require_operator),
):
    """Delete a saved Teams webhook."""
    await execute("DELETE FROM ms365_teams_webhooks WHERE id=%s", (webhook_id,))
    return {"ok": True}
