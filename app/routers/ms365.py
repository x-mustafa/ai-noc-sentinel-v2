"""
Microsoft 365 router — Outlook email + Teams messaging via Graph API.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
    generate_oauth_url,
    exchange_oauth_code,
    invalidate_token_cache,
    is_configured_async,
    get_ms365_email,
)
from app.database import fetch_all, fetch_one, execute

router = APIRouter()


def _raise_m365_list_error(rows: list[dict] | None) -> list[dict]:
    if rows and isinstance(rows, list) and isinstance(rows[0], dict) and rows[0].get("error"):
        raise HTTPException(503, rows[0]["error"])
    return rows or []


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


class M365ConfigBody(BaseModel):
    tenant_id:     Optional[str] = None
    client_id:     Optional[str] = None
    client_secret: Optional[str] = None   # blank = keep existing
    email:         Optional[str] = None


# ── MS365 Settings (DB-persisted credentials) ──────────────────────────────────

@router.get("/config")
async def get_m365_config(session: dict = Depends(get_session)):
    """Return current MS365 config (secrets masked). Admin/operator only."""
    from app.config import settings
    row = await fetch_one("SELECT * FROM zabbix_config LIMIT 1") or {}
    tenant_id     = row.get("ms365_tenant_id")     or settings.ms365_tenant_id or ""
    client_id     = row.get("ms365_client_id")     or settings.ms365_client_id or ""
    client_secret = row.get("ms365_client_secret") or settings.ms365_client_secret or ""
    email         = row.get("ms365_email")          or settings.ms365_email or ""
    oauth_token   = row.get("ms365_oauth_refresh_token") or ""
    oauth_email   = row.get("ms365_oauth_email") or ""
    return {
        "tenant_id":        tenant_id,
        "client_id":        client_id,
        "client_secret_set": bool(client_secret),
        "email":            email,
        "oauth_connected":  bool(oauth_token),
        "oauth_email":      oauth_email,
    }


@router.post("/config")
async def save_m365_config(
    body: M365ConfigBody,
    session: dict = Depends(require_operator),
):
    """Save MS365 app credentials to the database."""
    row = await fetch_one("SELECT ms365_client_secret FROM zabbix_config LIMIT 1") or {}
    existing_secret = row.get("ms365_client_secret") or ""
    secret = body.client_secret if body.client_secret else existing_secret

    updates = []
    params  = []
    if body.tenant_id is not None:
        updates.append("ms365_tenant_id=%s");     params.append(body.tenant_id)
    if body.client_id is not None:
        updates.append("ms365_client_id=%s");     params.append(body.client_id)
    if secret:
        updates.append("ms365_client_secret=%s"); params.append(secret)
    if body.email is not None:
        updates.append("ms365_email=%s");         params.append(body.email)

    if updates:
        await execute(f"UPDATE zabbix_config SET {', '.join(updates)}", params)
        invalidate_token_cache()

    return {"ok": True}


# ── OAuth 2.0 login flow ───────────────────────────────────────────────────────

@router.get("/oauth/start")
async def oauth_start(request: Request, session: dict = Depends(get_session)):
    """Redirect browser to Microsoft OAuth login page."""
    from app.config import settings
    row = await fetch_one("SELECT ms365_tenant_id, ms365_client_id FROM zabbix_config LIMIT 1") or {}
    tenant_id = row.get("ms365_tenant_id") or settings.ms365_tenant_id
    client_id = row.get("ms365_client_id") or settings.ms365_client_id

    if not tenant_id or not client_id:
        raise HTTPException(400, "MS365 Tenant ID and Client ID must be saved in Settings first")

    redirect_uri = str(request.base_url).rstrip("/") + "/api/ms365/oauth/callback"
    url = generate_oauth_url(tenant_id, client_id, redirect_uri)
    return RedirectResponse(url)


@router.get("/oauth/callback")
async def oauth_callback(request: Request, code: str = "", error: str = ""):
    """Exchange authorization code for tokens and return a popup-close page."""
    if error or not code:
        html = f"""<!DOCTYPE html><html><body>
<script>window.opener&&window.opener.postMessage({{type:'ms365_oauth_error',error:{repr(error)}}}, '*');window.close();</script>
<p>OAuth error: {error or 'No code received'}. You may close this window.</p>
</body></html>"""
        return HTMLResponse(html)

    from app.config import settings
    row = await fetch_one("SELECT * FROM zabbix_config LIMIT 1") or {}
    tenant_id     = row.get("ms365_tenant_id")     or settings.ms365_tenant_id
    client_id     = row.get("ms365_client_id")     or settings.ms365_client_id
    client_secret = row.get("ms365_client_secret") or settings.ms365_client_secret

    redirect_uri = str(request.base_url).rstrip("/") + "/api/ms365/oauth/callback"
    result = await exchange_oauth_code(
        code=code,
        redirect_uri=redirect_uri,
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )

    if not result.get("ok"):
        html = f"""<!DOCTYPE html><html><body>
<script>window.opener&&window.opener.postMessage({{type:'ms365_oauth_error',error:{repr(result.get('error','Unknown error'))}}}, '*');window.close();</script>
<p>Login failed: {result.get('error')}. You may close this window.</p>
</body></html>"""
        return HTMLResponse(html)

    email = result.get("oauth_email", "")
    html = f"""<!DOCTYPE html><html><head><title>Signed In</title></head><body>
<p style="font-family:sans-serif;padding:20px">✓ Signed in as <strong>{email}</strong>. Closing...</p>
<script>
  if (window.opener) {{
    window.opener.postMessage({{type:'ms365_oauth_done',email:{repr(email)}}}, '*');
  }}
  setTimeout(()=>window.close(), 1500);
</script>
</body></html>"""
    return HTMLResponse(html)


@router.get("/oauth/status")
async def oauth_status(session: dict = Depends(get_session)):
    """Return OAuth connection status."""
    row = await fetch_one("SELECT ms365_oauth_refresh_token, ms365_oauth_email FROM zabbix_config LIMIT 1") or {}
    oauth_token = row.get("ms365_oauth_refresh_token") or ""
    oauth_email = row.get("ms365_oauth_email") or ""
    configured  = await is_configured_async()
    return {
        "connected":     bool(oauth_token),
        "oauth_email":   oauth_email,
        "has_app_creds": configured,
    }


@router.delete("/oauth/disconnect")
async def oauth_disconnect(session: dict = Depends(require_operator)):
    """Remove stored OAuth tokens (disconnect delegated login)."""
    await execute(
        "UPDATE zabbix_config SET ms365_oauth_refresh_token=NULL, "
        "ms365_oauth_access_token=NULL, ms365_oauth_email=''",
    )
    invalidate_token_cache()
    return {"ok": True}


# ── Status / Test ──────────────────────────────────────────────────────────────

@router.get("/status")
async def m365_status(session: dict = Depends(get_session)):
    """Test Graph API connectivity and return configuration status."""
    result = await test_graph()
    email  = await get_ms365_email()
    return {
        "graph":      result,
        "ok":         result.get("ok", False),
        "email":      email or "(not configured)",
        "configured": result.get("ok", False),
        "error":      result.get("error", "") if not result.get("ok") else "",
        "hint":       result.get("hint", ""),
        "setup_guide": (
            "Required Azure setup:\n"
            "  1. Azure Portal → App Registrations → [app] → API Permissions\n"
            "  2. Add application permissions: Mail.Send + Mail.ReadWrite\n"
            "  3. Grant admin consent (Global Admin required)\n"
            "  4. Set credentials in Settings → Microsoft 365 section"
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
    return _raise_m365_list_error(await list_teams())


@router.get("/teams/{team_id}/channels")
async def api_list_channels(team_id: str, session: dict = Depends(get_session)):
    """List channels in a team. Requires Channel.ReadBasic.All."""
    return _raise_m365_list_error(await list_team_channels(team_id))


@router.get("/teams/{team_id}/channels/{channel_id}/messages")
async def api_get_channel_messages(
    team_id: str, channel_id: str, limit: int = 20,
    session: dict = Depends(get_session),
):
    """Get recent channel messages. Requires ChannelMessage.Read.All."""
    return _raise_m365_list_error(await get_channel_messages(team_id, channel_id, limit))


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
    return _raise_m365_list_error(await list_chats(limit))


@router.get("/chats/{chat_id}/messages")
async def api_get_chat_messages(
    chat_id: str, limit: int = 20,
    session: dict = Depends(get_session),
):
    """Get messages from a Teams chat. Requires Chat.ReadWrite.All."""
    return _raise_m365_list_error(await get_chat_messages(chat_id, limit))


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
