"""
Microsoft 365 integration via Graph API.

Supports two auth strategies (tried in order):
  1. OAuth delegated — if ms365_oauth_refresh_token is stored in DB
  2. App-only client credentials — using tenant_id/client_id/client_secret

Credentials are read from DB first (set via Settings UI), falling back to .env.

Token endpoint:
  POST https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token
"""
import asyncio
import logging
import time
from typing import Optional
from urllib.parse import urlencode

import httpx

from app.config import settings

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


async def _get_db_config() -> dict:
    """Read MS365 config columns from the zabbix_config row (DB-first credentials)."""
    try:
        from app.database import fetch_one
        row = await fetch_one("SELECT * FROM zabbix_config LIMIT 1")
        return row or {}
    except Exception:
        return {}


async def _has_oauth_connection() -> bool:
    cfg = await _get_db_config()
    return bool(cfg.get("ms365_oauth_refresh_token"))


async def _get_token() -> str:
    """Obtain (or return cached) Graph API access token.

    Tries OAuth refresh grant first (if refresh_token stored in DB),
    then falls back to app-only client_credentials.
    """
    from app.config import settings
    now = time.time()

    # Fast path: token still valid
    if _token_cache["token"] and now < _token_cache["expires_at"] - 300:
        return _token_cache["token"]

    # Slow path: refresh under lock so only one coroutine does the actual request
    async with _token_lock:
        if _token_cache["token"] and now < _token_cache["expires_at"] - 300:
            return _token_cache["token"]

        cfg = await _get_db_config()
        tenant_id     = cfg.get("ms365_tenant_id")     or settings.ms365_tenant_id
        client_id     = cfg.get("ms365_client_id")     or settings.ms365_client_id
        client_secret = cfg.get("ms365_client_secret") or settings.ms365_client_secret
        refresh_token = cfg.get("ms365_oauth_refresh_token") or ""

        base_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

        if refresh_token:
            data = {
                "grant_type":    "refresh_token",
                "client_id":     client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "scope":         "https://graph.microsoft.com/.default offline_access",
            }
        else:
            data = {
                "grant_type":    "client_credentials",
                "client_id":     client_id,
                "client_secret": client_secret,
                "scope":         "https://graph.microsoft.com/.default",
            }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(base_url, data=data)

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

        # Persist new refresh_token if returned (OAuth rotation)
        if refresh_token and j.get("refresh_token"):
            try:
                from app.database import execute
                await execute(
                    "UPDATE zabbix_config SET ms365_oauth_refresh_token=%s, "
                    "ms365_oauth_access_token=%s, ms365_oauth_token_expires=%s",
                    (j["refresh_token"], j["access_token"], int(now + int(j.get("expires_in", 3600)))),
                )
            except Exception as e:
                logger.warning(f"[M365] Could not persist refreshed OAuth token: {e}")

        return _token_cache["token"]


def invalidate_token_cache():
    """Clear the in-memory token so next request fetches fresh credentials."""
    _token_cache["token"] = None
    _token_cache["expires_at"] = 0


def _is_configured() -> bool:
    """Return True when .env has all four required M365 settings (sync fallback)."""
    from app.config import settings
    return bool(
        settings.ms365_tenant_id   and
        settings.ms365_client_id   and
        settings.ms365_client_secret and
        settings.ms365_email
    )


async def is_configured_async() -> bool:
    """Return True when MS365 is configured via DB or .env."""
    from app.config import settings
    cfg = await _get_db_config()
    return bool(
        (cfg.get("ms365_tenant_id")     or settings.ms365_tenant_id)   and
        (cfg.get("ms365_client_id")     or settings.ms365_client_id)   and
        (cfg.get("ms365_client_secret") or settings.ms365_client_secret) and
        (cfg.get("ms365_email")         or settings.ms365_email)
    )


async def get_ms365_email() -> str:
    """Return the configured shared mailbox email (DB first, then .env)."""
    from app.config import settings
    cfg = await _get_db_config()
    return cfg.get("ms365_email") or settings.ms365_email or ""


# ── OAuth helpers ──────────────────────────────────────────────────────────────

OAUTH_SCOPES = (
    "Mail.Send Mail.ReadWrite Chat.ReadWrite Chat.Read "
    "Team.ReadBasic.All ChannelMessage.Send offline_access"
)


def generate_oauth_url(tenant_id: str, client_id: str, redirect_uri: str) -> str:
    """Build the Microsoft OAuth 2.0 Authorization Code URL."""
    params = urlencode({
        "client_id":     client_id,
        "response_type": "code",
        "redirect_uri":  redirect_uri,
        "scope":         OAUTH_SCOPES,
        "response_mode": "query",
    })
    return f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize?{params}"


async def exchange_oauth_code(
    code: str,
    redirect_uri: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """Exchange authorization code for tokens and persist to DB."""
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type":    "authorization_code",
        "client_id":     client_id,
        "client_secret": client_secret,
        "code":          code,
        "redirect_uri":  redirect_uri,
        "scope":         OAUTH_SCOPES,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(url, data=data)

    if resp.status_code != 200:
        return {"ok": False, "error": f"Token exchange failed: {resp.text[:300]}"}

    j = resp.json()
    if "access_token" not in j:
        return {"ok": False, "error": f"No access_token: {j}"}

    access_token  = j["access_token"]
    refresh_token = j.get("refresh_token", "")
    expires_at    = int(time.time()) + int(j.get("expires_in", 3600))

    # Get the signed-in user's email via /me
    oauth_email = ""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            me = await c.get(
                f"{GRAPH}/me?$select=mail,userPrincipalName",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if me.status_code == 200:
                me_data    = me.json()
                oauth_email = me_data.get("mail") or me_data.get("userPrincipalName") or ""
    except Exception:
        pass

    # Persist to DB
    try:
        from app.database import execute
        await execute(
            "UPDATE zabbix_config SET "
            "ms365_oauth_refresh_token=%s, ms365_oauth_access_token=%s, "
            "ms365_oauth_token_expires=%s, ms365_oauth_email=%s",
            (refresh_token, access_token, expires_at, oauth_email),
        )
        # Update in-memory cache
        _token_cache["token"]      = access_token
        _token_cache["expires_at"] = expires_at
        invalidate_token_cache()  # force re-read next time (so DB row is used)
    except Exception as e:
        logger.error(f"[M365] Failed to persist OAuth tokens: {e}")
        return {"ok": False, "error": str(e)}

    return {"ok": True, "oauth_email": oauth_email}


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
    if not await is_configured_async():
        return {"ok": False, "error": "M365 not configured — set MS365_* credentials in Settings"}

    ms365_email = await get_ms365_email()
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
            "address": ms365_email,
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
        url     = f"{GRAPH}/users/{ms365_email}/sendMail"

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
    if not await is_configured_async():
        return []

    ms365_email = await get_ms365_email()
    try:
        token   = await _get_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = (
            f"{GRAPH}/users/{ms365_email}/mailFolders/inbox/messages"
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
    if not await is_configured_async():
        return {"ok": False, "error": "M365 not configured"}

    ms365_email = await get_ms365_email()
    try:
        token   = await _get_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = (
            f"{GRAPH}/users/{ms365_email}/messages/{message_id}"
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
    if not await is_configured_async():
        return {"ok": False, "error": "M365 not configured"}

    ms365_email = await get_ms365_email()
    try:
        token   = await _get_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url     = f"{GRAPH}/users/{ms365_email}/messages/{message_id}"
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
    if not await is_configured_async():
        return {"ok": False, "error": "M365 not configured"}

    ms365_email = await get_ms365_email()
    emp_name = EMPLOYEE_NAMES.get(employee_id, employee_id.upper())

    try:
        token   = await _get_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url     = f"{GRAPH}/users/{ms365_email}/messages/{message_id}/reply"
        payload = {
            "message": {
                "from": {
                    "emailAddress": {
                        "name":    f"{emp_name} via NOC Sentinel",
                        "address": ms365_email,
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
    if not await is_configured_async():
        return []
    try:
        token = await _get_token()
        url   = f"{GRAPH}/teams?$select=id,displayName,description&$top=50"
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            return [{"error": f"Graph {resp.status_code}"}]
        return [
            {
                "id": t["id"],
                "name": t.get("displayName", ""),
                "displayName": t.get("displayName", ""),
                "description": t.get("description", ""),
            }
            for t in resp.json().get("value", [])
        ]
    except Exception as e:
        return [{"error": str(e)}]


async def list_team_channels(team_id: str) -> list[dict]:
    """List channels in a team. Requires Channel.ReadBasic.All."""
    if not await is_configured_async():
        return []
    try:
        token = await _get_token()
        url   = f"{GRAPH}/teams/{team_id}/channels?$select=id,displayName,membershipType"
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            return [{"error": f"Graph {resp.status_code}"}]
        return [
            {
                "id": ch["id"],
                "name": ch.get("displayName", ""),
                "displayName": ch.get("displayName", ""),
                "type": ch.get("membershipType", "standard"),
                "membershipType": ch.get("membershipType", "standard"),
            }
            for ch in resp.json().get("value", [])
        ]
    except Exception as e:
        return [{"error": str(e)}]


async def list_chats(limit: int = 50) -> list[dict]:
    """List recent Teams chats. Requires delegated Chat permissions."""
    if not await is_configured_async():
        return []
    if not await _has_oauth_connection():
        return [{
            "error": (
                "Teams group chats require delegated Microsoft 365 OAuth sign-in. "
                "Open Settings > Microsoft 365 and connect an account with Chat permissions."
            )
        }]
    try:
        token = await _get_token()
        url   = f"{GRAPH}/me/chats?$top={max(1, min(limit, 50))}"
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            try:
                err = resp.json().get("error", {})
                msg = f"{err.get('code','')}: {err.get('message','')}"
            except Exception:
                msg = resp.text[:200]
            return [{"error": f"Graph {resp.status_code} — {msg}"}]
        chats = []
        for ch in resp.json().get("value", []):
            chat_type = ch.get("chatType", "") or "unknown"
            if chat_type == "oneOnOne":
                display_name = ch.get("topic") or "Direct Chat"
            else:
                display_name = ch.get("topic") or f"Chat ({chat_type})"
            chats.append(
                {
                    "id": ch["id"],
                    "topic": display_name,
                    "chatType": chat_type,
                    "lastUpdatedDateTime": ch.get("lastUpdatedDateTime", ""),
                    "name": display_name,
                    "type": chat_type,
                    "updated": ch.get("lastUpdatedDateTime", ""),
                }
            )
        chats.sort(key=lambda ch: ch.get("lastUpdatedDateTime", ""), reverse=True)
        return chats[:limit]
    except Exception as e:
        return [{"error": str(e)}]


async def get_chat_messages(chat_id: str, limit: int = 20) -> list[dict]:
    """Get recent messages from a Teams chat. Requires Chat.ReadWrite.All."""
    if not await is_configured_async():
        return []
    if not await _has_oauth_connection():
        return [{
            "error": (
                "Teams group chats require delegated Microsoft 365 OAuth sign-in. "
                "Open Settings > Microsoft 365 and connect an account with Chat permissions."
            )
        }]
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
                "id": m.get("id", ""),
                "from": m.get("from", {}),
                "body": m.get("body", {}),
                "createdDateTime": m.get("createdDateTime", ""),
                "messageType": m.get("messageType", ""),
                "sender": m.get("from", {}).get("user", {}).get("displayName", "") or
                          m.get("from", {}).get("application", {}).get("displayName", ""),
                "text": m.get("body", {}).get("content", ""),
                "date": m.get("createdDateTime", ""),
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
    if not await is_configured_async():
        return {"ok": False, "error": "M365 not configured"}
    if not await _has_oauth_connection():
        return {
            "ok": False,
            "error": (
                "Teams group chats require delegated Microsoft 365 OAuth sign-in. "
                "Open Settings > Microsoft 365 and connect an account with Chat permissions."
            ),
        }
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
    if not await is_configured_async():
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
    if not await is_configured_async():
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
        async with httpx.AsyncClient(timeout=15, verify=settings.outbound_tls_verify) as client:
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
    if not await is_configured_async():
        return {
            "ok":    False,
            "error": "MS365 not configured — set credentials in Settings → Microsoft 365",
        }

    ms365_email = await get_ms365_email()
    try:
        token   = await _get_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = (
            f"{GRAPH}/users/{ms365_email}/mailFolders/inbox"
            f"?$select=id,displayName,totalItemCount"
        )
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)

        if resp.status_code == 200:
            folder = resp.json()
            return {
                "ok":          True,
                "mailbox":     ms365_email,
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
