"""
Microsoft 365 integration via Graph API (app-only / client credentials).

Permissions required on the Azure App Registration:
  - Mail.Send          (Application)
  - Mail.ReadWrite     (Application)
  - User.Read.All      (Application — optional, for display)

Token endpoint:
  POST https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token
"""
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

# ── Token cache (in-memory, refreshed 5 min before expiry) ────────────────────

_token_cache: dict = {"token": None, "expires_at": 0}


async def _get_token() -> str:
    """Obtain (or return cached) Graph API access token via client credentials."""
    from app.config import settings
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 300:
        return _token_cache["token"]

    url = f"https://login.microsoftonline.com/{settings.ms365_tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type":    "client_credentials",
        "client_id":     settings.ms365_client_id,
        "client_secret": settings.ms365_client_secret,
        "scope":         "https://graph.microsoft.com/.default",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, data=data)
    if resp.status_code != 200:
        raise RuntimeError(f"Token fetch failed {resp.status_code}: {resp.text[:300]}")
    j = resp.json()
    _token_cache["token"]      = j["access_token"]
    _token_cache["expires_at"] = now + int(j.get("expires_in", 3600))
    return _token_cache["token"]


def _is_configured() -> bool:
    from app.config import settings
    return bool(settings.ms365_tenant_id and settings.ms365_client_id and settings.ms365_client_secret)


# ── Send email via Graph API ──────────────────────────────────────────────────

async def send_email(
    to: str | list[str],
    subject: str,
    body: str,
    employee_id: str = "aria",
    html: bool = False,
) -> dict:
    """Send an email on behalf of the shared NOC mailbox using Graph API."""
    from app.config import settings
    if not _is_configured():
        return {"ok": False, "error": "Graph API credentials not configured"}
    if not settings.ms365_email:
        return {"ok": False, "error": "MS365_EMAIL not set"}

    to_list = [to] if isinstance(to, str) else to
    emp_name = EMPLOYEE_NAMES.get(employee_id, employee_id.upper())

    payload = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "HTML" if html else "Text",
                "content": body,
            },
            "toRecipients": [
                {"emailAddress": {"address": addr}} for addr in to_list
            ],
            "from": {
                "emailAddress": {
                    "name":    f"{emp_name} via NOC Sentinel",
                    "address": settings.ms365_email,
                }
            },
        },
        "saveToSentItems": "true",
    }

    try:
        token = await _get_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{GRAPH}/users/{settings.ms365_email}/sendMail"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code == 202:
            logger.info(f"Email sent to {to_list} from {employee_id}")
            return {"ok": True, "to": to_list, "subject": subject}
        return {"ok": False, "error": f"Graph {resp.status_code}: {resp.text[:300]}"}
    except Exception as e:
        logger.error(f"Graph send_email failed: {e}")
        return {"ok": False, "error": str(e)}


# ── Read inbox via Graph API ───────────────────────────────────────────────────

async def get_inbox(limit: int = 20) -> list[dict]:
    """Fetch recent emails from the shared NOC mailbox."""
    from app.config import settings
    if not _is_configured():
        return [{"error": "Graph API credentials not configured"}]
    if not settings.ms365_email:
        return [{"error": "MS365_EMAIL not set"}]

    try:
        token = await _get_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = (
            f"{GRAPH}/users/{settings.ms365_email}/mailFolders/inbox/messages"
            f"?$top={limit}&$orderby=receivedDateTime desc"
            f"&$select=id,subject,from,receivedDateTime,bodyPreview,isRead"
        )
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return [{"error": f"Graph {resp.status_code}: {resp.text[:200]}"}]
        messages = resp.json().get("value", [])
        return [
            {
                "id":        m.get("id", ""),
                "subject":   m.get("subject", "(no subject)"),
                "from":      m.get("from", {}).get("emailAddress", {}).get("address", ""),
                "from_name": m.get("from", {}).get("emailAddress", {}).get("name", ""),
                "date":      m.get("receivedDateTime", ""),
                "preview":   m.get("bodyPreview", "")[:300],
                "is_read":   m.get("isRead", True),
            }
            for m in messages
        ]
    except Exception as e:
        logger.error(f"Graph get_inbox failed: {e}")
        return [{"error": str(e)}]


# ── Teams message via incoming webhook (no Graph API needed) ──────────────────

async def send_teams_message(
    webhook_url: str,
    message: str,
    title: str = "",
    employee_id: str = "aria",
) -> dict:
    """Send a message to a Microsoft Teams channel via incoming webhook."""
    if not webhook_url:
        return {"ok": False, "error": "No Teams webhook URL configured"}

    emp_name   = EMPLOYEE_NAMES.get(employee_id, employee_id.upper())
    emp_colors = {"aria": "00D4FF", "nexus": "A855F7", "cipher": "FF8C00", "vega": "4ADE80"}

    card_body = []
    if title:
        card_body.append({"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium"})
    card_body.append({"type": "TextBlock", "text": message, "wrap": True, "color": "Default"})
    card_body.append({
        "type": "TextBlock",
        "text": f"— {emp_name} | NOC Sentinel",
        "size": "Small", "color": "Accent", "isSubtle": True,
    })

    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": card_body,
                "msteams": {"width": "Full"},
            },
        }],
    }

    try:
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            resp = await client.post(webhook_url, json=payload,
                                     headers={"Content-Type": "application/json"})
        if resp.status_code in (200, 202):
            return {"ok": True, "status": resp.status_code}
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Test Graph API connectivity ────────────────────────────────────────────────

async def test_graph() -> dict:
    """Test Graph API token acquisition and basic mailbox access."""
    from app.config import settings
    if not _is_configured():
        return {"ok": False, "error": "Graph API credentials not configured"}
    try:
        token = await _get_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{GRAPH}/users/{settings.ms365_email}?$select=displayName,mail,userPrincipalName"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            u = resp.json()
            return {
                "ok":          True,
                "displayName": u.get("displayName", ""),
                "mail":        u.get("mail") or u.get("userPrincipalName", ""),
                "message":     "Graph API connected successfully",
            }
        return {"ok": False, "error": f"Graph {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
