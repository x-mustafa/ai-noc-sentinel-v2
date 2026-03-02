"""
Microsoft 365 integration via Graph API (app-only / client credentials).

Required Azure App Registration permissions (Application, with admin consent):
  - Mail.Send        — send emails from the NOC shared mailbox
  - Mail.ReadWrite   — read + manage inbox

Token endpoint:
  POST https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token
"""
import asyncio
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"

EMPLOYEE_NAMES = {
    "aria":   "ARIA — NOC Analyst",
    "nexus":  "NEXUS — Infrastructure Engineer",
    "cipher": "CIPHER — Security Analyst",
    "vega":   "VEGA — Site Reliability Engineer",
}

# ── Token cache (in-memory, protected by a lock against concurrent refreshes) ──

_token_cache: dict = {"token": None, "expires_at": 0}
_token_lock = asyncio.Lock()


async def _get_token() -> str:
    """Obtain (or return cached) Graph API access token via client credentials."""
    from app.config import settings
    now = time.time()

    # Fast path: token still valid
    if _token_cache["token"] and now < _token_cache["expires_at"] - 300:
        return _token_cache["token"]

    # Slow path: refresh under lock so only one coroutine does the actual request
    async with _token_lock:
        # Re-check inside the lock (another coroutine may have already refreshed)
        if _token_cache["token"] and now < _token_cache["expires_at"] - 300:
            return _token_cache["token"]

        url = (
            f"https://login.microsoftonline.com/"
            f"{settings.ms365_tenant_id}/oauth2/v2.0/token"
        )
        data = {
            "grant_type":    "client_credentials",
            "client_id":     settings.ms365_client_id,
            "client_secret": settings.ms365_client_secret,
            "scope":         "https://graph.microsoft.com/.default",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, data=data)

        if resp.status_code != 200:
            body = resp.text[:400]
            raise RuntimeError(
                f"Token fetch failed (HTTP {resp.status_code}): {body}"
            )

        j = resp.json()
        if "access_token" not in j:
            raise RuntimeError(f"No access_token in response: {j}")

        _token_cache["token"]      = j["access_token"]
        _token_cache["expires_at"] = now + int(j.get("expires_in", 3600))
        return _token_cache["token"]


def _is_configured() -> bool:
    """Return True only when all four required M365 settings are non-empty."""
    from app.config import settings
    return bool(
        settings.ms365_tenant_id   and
        settings.ms365_client_id   and
        settings.ms365_client_secret and
        settings.ms365_email
    )


# ── Send email via Graph API ───────────────────────────────────────────────────

async def send_email(
    to: "str | list[str]",
    subject: str,
    body: str,
    employee_id: str = "aria",
    html: bool = False,
    cc: "str | list[str] | None" = None,
) -> dict:
    """
    Send an email from the shared NOC mailbox using Graph API sendMail.
    Requires Mail.Send application permission with admin consent.
    """
    from app.config import settings
    if not _is_configured():
        return {"ok": False, "error": "M365 not configured — set MS365_* variables in .env"}

    to_list = [to] if isinstance(to, str) else list(to)
    to_list = [t.strip() for t in to_list if t.strip()]
    if not to_list:
        return {"ok": False, "error": "No valid recipients"}

    emp_name = EMPLOYEE_NAMES.get(employee_id, employee_id.upper())

    message: dict = {
        "subject": subject,
        "body": {
            "contentType": "HTML" if html else "Text",
            "content": body,
        },
        "toRecipients": [
            {"emailAddress": {"address": addr}} for addr in to_list
        ],
    }

    # Custom display name for the sender (address must match the mailbox)
    message["from"] = {
        "emailAddress": {
            "name":    f"{emp_name} via NOC Sentinel",
            "address": settings.ms365_email,
        }
    }

    if cc:
        cc_list = [cc] if isinstance(cc, str) else list(cc)
        cc_list = [a.strip() for a in cc_list if a.strip()]
        if cc_list:
            message["ccRecipients"] = [
                {"emailAddress": {"address": addr}} for addr in cc_list
            ]

    payload = {
        "message":          message,
        "saveToSentItems":  True,      # bool, not "true" string
    }

    try:
        token   = await _get_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url     = f"{GRAPH}/users/{settings.ms365_email}/sendMail"

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json=payload, headers=headers)

        if resp.status_code == 202:
            logger.info(f"[M365] Email sent to {to_list} by {employee_id}")
            return {"ok": True, "to": to_list, "subject": subject}

        # Graph returns 4xx/5xx with an error body — surface it
        try:
            err_body = resp.json().get("error", {})
            err_msg  = f"{err_body.get('code','')}: {err_body.get('message','')}"
        except Exception:
            err_msg = resp.text[:300]
        logger.error(f"[M365] sendMail failed HTTP {resp.status_code}: {err_msg}")
        return {"ok": False, "error": f"Graph HTTP {resp.status_code} — {err_msg}"}

    except Exception as e:
        logger.error(f"[M365] send_email exception: {e}")
        return {"ok": False, "error": str(e)}


# ── Read inbox via Graph API ───────────────────────────────────────────────────

async def get_inbox(limit: int = 20) -> list[dict]:
    """
    Fetch recent emails from the shared NOC mailbox.
    Requires Mail.ReadWrite application permission.
    """
    from app.config import settings
    if not _is_configured():
        return []

    try:
        token   = await _get_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = (
            f"{GRAPH}/users/{settings.ms365_email}/mailFolders/inbox/messages"
            f"?$top={min(limit, 50)}"
            f"&$orderby=receivedDateTime desc"
            f"&$select=id,subject,from,receivedDateTime,bodyPreview,isRead,hasAttachments"
        )
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers)

        if resp.status_code != 200:
            try:
                err = resp.json().get("error", {})
                msg = f"{err.get('code','')}: {err.get('message','')}"
            except Exception:
                msg = resp.text[:200]
            logger.error(f"[M365] get_inbox failed HTTP {resp.status_code}: {msg}")
            return [{"error": f"Graph HTTP {resp.status_code} — {msg}"}]

        messages = resp.json().get("value", [])
        return [
            {
                "id":             m.get("id", ""),
                "subject":        m.get("subject", "(no subject)"),
                "from":           m.get("from", {}).get("emailAddress", {}).get("address", ""),
                "from_name":      m.get("from", {}).get("emailAddress", {}).get("name", ""),
                "date":           m.get("receivedDateTime", ""),
                "preview":        m.get("bodyPreview", "")[:300],
                "is_read":        m.get("isRead", True),
                "has_attachments": m.get("hasAttachments", False),
            }
            for m in messages
        ]
    except Exception as e:
        logger.error(f"[M365] get_inbox exception: {e}")
        return [{"error": str(e)}]


async def get_email_body(message_id: str) -> dict:
    """
    Fetch the full body of a single email by its Graph message ID.
    Requires Mail.ReadWrite application permission.
    """
    from app.config import settings
    if not _is_configured():
        return {"ok": False, "error": "M365 not configured"}

    try:
        token   = await _get_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = (
            f"{GRAPH}/users/{settings.ms365_email}/messages/{message_id}"
            f"?$select=id,subject,from,toRecipients,receivedDateTime,"
            f"body,bodyPreview,isRead,hasAttachments"
        )
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers)

        if resp.status_code != 200:
            try:
                err = resp.json().get("error", {})
                msg = f"{err.get('code','')}: {err.get('message','')}"
            except Exception:
                msg = resp.text[:200]
            return {"ok": False, "error": f"Graph HTTP {resp.status_code} — {msg}"}

        m = resp.json()
        return {
            "ok":          True,
            "id":          m.get("id", ""),
            "subject":     m.get("subject", "(no subject)"),
            "from":        m.get("from", {}).get("emailAddress", {}).get("address", ""),
            "from_name":   m.get("from", {}).get("emailAddress", {}).get("name", ""),
            "to":          [
                r.get("emailAddress", {}).get("address", "")
                for r in m.get("toRecipients", [])
            ],
            "date":        m.get("receivedDateTime", ""),
            "body":        m.get("body", {}).get("content", ""),
            "body_type":   m.get("body", {}).get("contentType", "Text"),
            "is_read":     m.get("isRead", True),
        }
    except Exception as e:
        logger.error(f"[M365] get_email_body exception: {e}")
        return {"ok": False, "error": str(e)}


async def mark_as_read(message_id: str) -> dict:
    """Mark an email as read. Requires Mail.ReadWrite."""
    from app.config import settings
    if not _is_configured():
        return {"ok": False, "error": "M365 not configured"}

    try:
        token   = await _get_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url     = f"{GRAPH}/users/{settings.ms365_email}/messages/{message_id}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(url, json={"isRead": True}, headers=headers)
        if resp.status_code == 200:
            return {"ok": True}
        try:
            err = resp.json().get("error", {})
            msg = f"{err.get('code','')}: {err.get('message','')}"
        except Exception:
            msg = resp.text[:200]
        return {"ok": False, "error": f"Graph HTTP {resp.status_code} — {msg}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def reply_to_email(
    message_id: str,
    reply_body: str,
    employee_id: str = "aria",
) -> dict:
    """
    Reply to an existing email thread using Graph API replyAll.
    Requires Mail.Send application permission.
    """
    from app.config import settings
    if not _is_configured():
        return {"ok": False, "error": "M365 not configured"}

    emp_name = EMPLOYEE_NAMES.get(employee_id, employee_id.upper())

    try:
        token   = await _get_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url     = f"{GRAPH}/users/{settings.ms365_email}/messages/{message_id}/reply"
        payload = {
            "message": {
                "from": {
                    "emailAddress": {
                        "name":    f"{emp_name} via NOC Sentinel",
                        "address": settings.ms365_email,
                    }
                }
            },
            "comment": reply_body,
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json=payload, headers=headers)

        if resp.status_code == 202:
            logger.info(f"[M365] Reply sent to message {message_id} by {employee_id}")
            return {"ok": True}

        try:
            err = resp.json().get("error", {})
            msg = f"{err.get('code','')}: {err.get('message','')}"
        except Exception:
            msg = resp.text[:300]
        return {"ok": False, "error": f"Graph HTTP {resp.status_code} — {msg}"}

    except Exception as e:
        logger.error(f"[M365] reply_to_email exception: {e}")
        return {"ok": False, "error": str(e)}


# ── Teams chats & channels via Graph API ──────────────────────────────────────

async def list_teams() -> list[dict]:
    """List all teams the app can see. Requires Team.ReadBasic.All."""
    from app.config import settings
    if not _is_configured():
        return []
    try:
        token = await _get_token()
        url   = f"{GRAPH}/teams?$select=id,displayName,description&$top=50"
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            return [{"error": f"Graph {resp.status_code}"}]
        return [
            {"id": t["id"], "name": t.get("displayName",""), "description": t.get("description","")}
            for t in resp.json().get("value", [])
        ]
    except Exception as e:
        return [{"error": str(e)}]


async def list_team_channels(team_id: str) -> list[dict]:
    """List channels in a team. Requires Channel.ReadBasic.All."""
    from app.config import settings
    if not _is_configured():
        return []
    try:
        token = await _get_token()
        url   = f"{GRAPH}/teams/{team_id}/channels?$select=id,displayName,membershipType"
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            return [{"error": f"Graph {resp.status_code}"}]
        return [
            {"id": ch["id"], "name": ch.get("displayName",""), "type": ch.get("membershipType","standard")}
            for ch in resp.json().get("value", [])
        ]
    except Exception as e:
        return [{"error": str(e)}]


async def list_chats(limit: int = 50) -> list[dict]:
    """List group chats and meetings. Requires Chat.ReadWrite.All."""
    from app.config import settings
    if not _is_configured():
        return []
    try:
        token = await _get_token()
        url   = (
            f"{GRAPH}/users/{settings.ms365_email}/chats"
            f"?$filter=chatType eq 'group' or chatType eq 'meeting'"
            f"&$select=id,topic,chatType,lastUpdatedDateTime"
            f"&$top={limit}&$orderby=lastUpdatedDateTime desc"
        )
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            try:
                err = resp.json().get("error", {})
                msg = f"{err.get('code','')}: {err.get('message','')}"
            except Exception:
                msg = resp.text[:200]
            return [{"error": f"Graph {resp.status_code} — {msg}"}]
        return [
            {
                "id":       ch["id"],
                "name":     ch.get("topic") or f"Group Chat ({ch.get('chatType','')})",
                "type":     ch.get("chatType",""),
                "updated":  ch.get("lastUpdatedDateTime",""),
            }
            for ch in resp.json().get("value", [])
        ]
    except Exception as e:
        return [{"error": str(e)}]


async def get_chat_messages(chat_id: str, limit: int = 20) -> list[dict]:
    """Get recent messages from a Teams chat. Requires Chat.ReadWrite.All."""
    from app.config import settings
    if not _is_configured():
        return []
    try:
        token = await _get_token()
        url   = (
            f"{GRAPH}/chats/{chat_id}/messages"
            f"?$top={limit}"
            f"&$select=id,from,body,createdDateTime,messageType"
        )
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            return [{"error": f"Graph {resp.status_code}"}]
        msgs = resp.json().get("value", [])
        return [
            {
                "id":      m.get("id",""),
                "from":    m.get("from",{}).get("user",{}).get("displayName","") or
                           m.get("from",{}).get("application",{}).get("displayName",""),
                "body":    m.get("body",{}).get("content",""),
                "type":    m.get("body",{}).get("contentType","text"),
                "date":    m.get("createdDateTime",""),
            }
            for m in msgs if m.get("messageType","") == "message"
        ]
    except Exception as e:
        return [{"error": str(e)}]


async def send_to_chat(
    chat_id: str,
    message: str,
    employee_id: str = "aria",
) -> dict:
    """Send a message to a Teams chat via Graph API. Requires Chat.ReadWrite.All."""
    from app.config import settings
    if not _is_configured():
        return {"ok": False, "error": "M365 not configured"}
    emp_name = EMPLOYEE_NAMES.get(employee_id, employee_id.upper())
    html_body = message.replace("\n", "<br>") + f"<br><em>— {emp_name} | NOC Sentinel</em>"
    try:
        token = await _get_token()
        url   = f"{GRAPH}/chats/{chat_id}/messages"
        payload = {"body": {"contentType": "html", "content": html_body}}
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.post(url, json=payload,
                                headers={"Authorization": f"Bearer {token}",
                                         "Content-Type": "application/json"})
        if resp.status_code == 201:
            logger.info(f"[M365] Sent to chat {chat_id} by {employee_id}")
            return {"ok": True}
        try:
            err = resp.json().get("error", {})
            msg = f"{err.get('code','')}: {err.get('message','')}"
        except Exception:
            msg = resp.text[:200]
        return {"ok": False, "error": f"Graph {resp.status_code} — {msg}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def send_to_channel(
    team_id: str,
    channel_id: str,
    message: str,
    title: str = "",
    employee_id: str = "aria",
) -> dict:
    """Send a message to a Teams channel via Graph API. Requires ChannelMessage.Send."""
    from app.config import settings
    if not _is_configured():
        return {"ok": False, "error": "M365 not configured"}
    emp_name = EMPLOYEE_NAMES.get(employee_id, employee_id.upper())
    heading  = f"<h3>{title}</h3>" if title else ""
    html_body = heading + message.replace("\n", "<br>") + f"<br><em>— {emp_name} | NOC Sentinel</em>"
    try:
        token = await _get_token()
        url   = f"{GRAPH}/teams/{team_id}/channels/{channel_id}/messages"
        payload = {"body": {"contentType": "html", "content": html_body}}
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.post(url, json=payload,
                                headers={"Authorization": f"Bearer {token}",
                                         "Content-Type": "application/json"})
        if resp.status_code == 201:
            logger.info(f"[M365] Sent to channel {channel_id} by {employee_id}")
            return {"ok": True}
        try:
            err = resp.json().get("error", {})
            msg = f"{err.get('code','')}: {err.get('message','')}"
        except Exception:
            msg = resp.text[:200]
        return {"ok": False, "error": f"Graph {resp.status_code} — {msg}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def get_channel_messages(team_id: str, channel_id: str, limit: int = 20) -> list[dict]:
    """Get recent messages from a Teams channel. Requires ChannelMessage.Read.All."""
    from app.config import settings
    if not _is_configured():
        return []
    try:
        token = await _get_token()
        url   = (
            f"{GRAPH}/teams/{team_id}/channels/{channel_id}/messages"
            f"?$top={limit}&$select=id,from,body,createdDateTime,messageType"
        )
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            return [{"error": f"Graph {resp.status_code}"}]
        msgs = resp.json().get("value", [])
        return [
            {
                "id":   m.get("id",""),
                "from": m.get("from",{}).get("user",{}).get("displayName",""),
                "body": m.get("body",{}).get("content",""),
                "type": m.get("body",{}).get("contentType","text"),
                "date": m.get("createdDateTime",""),
            }
            for m in msgs if m.get("messageType","") == "message"
        ]
    except Exception as e:
        return [{"error": str(e)}]


# ── Teams message via incoming webhook ────────────────────────────────────────

async def send_teams_message(
    webhook_url: str,
    message: str,
    title: str = "",
    employee_id: str = "aria",
) -> dict:
    """
    Send a message to a Microsoft Teams channel via incoming webhook.
    Supports both new-style Workflow (Power Automate) and legacy connectors.
    """
    if not webhook_url:
        return {"ok": False, "error": "No Teams webhook URL configured"}

    emp_name = EMPLOYEE_NAMES.get(employee_id, employee_id.upper())

    card_body = []
    if title:
        card_body.append({
            "type":   "TextBlock",
            "text":   title,
            "weight": "Bolder",
            "size":   "Medium",
            "wrap":   True,
        })

    # Split message into paragraphs for better readability in Teams
    paragraphs = [p.strip() for p in message.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [message]

    for para in paragraphs[:10]:    # limit to 10 paragraphs
        card_body.append({
            "type": "TextBlock",
            "text": para[:1000],
            "wrap": True,
        })

    card_body.append({
        "type":     "TextBlock",
        "text":     f"— {emp_name} | NOC Sentinel",
        "size":     "Small",
        "isSubtle": True,
        "wrap":     True,
    })

    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl":  None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type":    "AdaptiveCard",
                "version": "1.5",
                "body":    card_body,
            },
        }],
    }

    try:
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            resp = await client.post(
                webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )

        # New Workflow connectors return 202; legacy O365 connectors return "1" with 200
        if resp.status_code in (200, 202):
            return {"ok": True, "status": resp.status_code}

        logger.error(f"[M365] Teams webhook HTTP {resp.status_code}: {resp.text[:200]}")
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    except Exception as e:
        logger.error(f"[M365] send_teams_message exception: {e}")
        return {"ok": False, "error": str(e)}


# ── Test Graph API connectivity ────────────────────────────────────────────────

async def test_graph() -> dict:
    """
    Test Graph API token acquisition and mailbox access.
    Requires Mail.ReadWrite application permission.
    """
    from app.config import settings

    if not _is_configured():
        missing = []
        if not settings.ms365_tenant_id:   missing.append("MS365_TENANT_ID")
        if not settings.ms365_client_id:   missing.append("MS365_CLIENT_ID")
        if not settings.ms365_client_secret: missing.append("MS365_CLIENT_SECRET")
        if not settings.ms365_email:        missing.append("MS365_EMAIL")
        return {
            "ok":    False,
            "error": f"Missing configuration: {', '.join(missing)}",
        }

    try:
        token   = await _get_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = (
            f"{GRAPH}/users/{settings.ms365_email}/mailFolders/inbox"
            f"?$select=id,displayName,totalItemCount"
        )
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)

        if resp.status_code == 200:
            folder = resp.json()
            return {
                "ok":          True,
                "mailbox":     settings.ms365_email,
                "inbox_count": folder.get("totalItemCount", "?"),
                "message":     "Graph API connected — mailbox accessible",
            }

        # Parse error once — avoid calling resp.json() twice
        err_body = {}
        try:
            err_body = resp.json().get("error", {})
        except Exception:
            pass
        code = err_body.get("code", "")
        msg  = f"{code}: {err_body.get('message', resp.text[:300])}" if code else resp.text[:300]

        # Map known Graph error codes to actionable fix instructions
        _HINTS = {
            "ErrorAccessDenied": (
                "Admin consent not granted. Fix in Azure Portal:\n"
                "  1. Azure Active Directory → App Registrations → [your app]\n"
                "  2. API Permissions → Add a permission → Microsoft Graph → Application permissions\n"
                "  3. Add: Mail.Send  AND  Mail.ReadWrite\n"
                "  4. Click 'Grant admin consent for [your tenant]' (requires Global Admin)\n"
                "  5. Both permissions must show a green ✓ status before retrying."
            ),
            "Authorization_RequestDenied": (
                "Admin consent required. Go to Azure Portal → App Registrations → "
                "API Permissions and click 'Grant admin consent for [tenant]'."
            ),
            "AuthenticationError": (
                "Token rejected — double-check MS365_CLIENT_ID, MS365_CLIENT_SECRET, "
                "and MS365_TENANT_ID in your .env file."
            ),
            "InvalidAuthenticationToken": (
                "Token is expired or malformed. Restart the server to force a token refresh. "
                "If it persists, verify MS365_CLIENT_SECRET hasn't expired in Azure Portal."
            ),
            "Request_ResourceNotFound": (
                "Mailbox not found. Verify MS365_EMAIL is a valid, licensed mailbox "
                "(or shared mailbox) that exists in your tenant."
            ),
            "MailboxNotEnabledForRESTAPI": (
                "This mailbox type doesn't support Graph API. "
                "Use a regular user mailbox or a mail-enabled shared mailbox."
            ),
        }
        hint = _HINTS.get(code, "")

        return {
            "ok":    False,
            "error": f"Graph HTTP {resp.status_code} — {msg}",
            "hint":  hint,
        }

    except RuntimeError as e:
        # Token fetch failed — provide actionable hint
        return {
            "ok":    False,
            "error": str(e),
            "hint":  "Check MS365_TENANT_ID, MS365_CLIENT_ID, MS365_CLIENT_SECRET in .env",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
