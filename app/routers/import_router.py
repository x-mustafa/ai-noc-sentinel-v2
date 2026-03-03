import json
import math
import base64
import re
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional, List, Any

import httpx

from app.config import settings
from app.deps import get_session, require_admin, require_operator
from app.database import fetch_one, execute
from app.services.zabbix_client import call_zabbix

router = APIRouter()


@router.post("/analyze")
async def analyze_diagram(
    map: UploadFile = File(...),
    session: dict = Depends(require_operator),
):
    cfg = await fetch_one("SELECT claude_key FROM zabbix_config LIMIT 1") or {}
    claude_key = cfg.get("claude_key", "")
    if not claude_key:
        raise HTTPException(400, "Claude API key not set — go to Settings → AI Providers")

    allowed_types = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}
    if map.content_type not in allowed_types:
        raise HTTPException(400, f"Unsupported file type: {map.content_type}")

    raw  = await map.read()
    b64  = base64.b64encode(raw).decode()

    if map.content_type == "application/pdf":
        media_block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    else:
        media_block = {"type": "image",    "source": {"type": "base64", "media_type": map.content_type, "data": b64}}

    prompt = (
        "This is a network topology diagram. Extract EVERY network device/node visible in the diagram.\n\n"
        "For each device return these exact fields:\n"
        "- name: the device label/hostname shown in the diagram\n"
        "- ip: the IP address shown (empty string \"\" if none visible)\n"
        "- type: one of: router, switch, firewall, server, load_balancer, storage, endpoint, other\n\n"
        "Return ONLY a valid JSON array with no markdown, no explanation, no code fences.\n"
        'Example: [{"name":"Core-SW-01","ip":"10.0.0.1","type":"switch"}]'
    )

    async with httpx.AsyncClient(verify=settings.outbound_tls_verify, timeout=90) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": claude_key, "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json"},
            json={"model": "claude-opus-4-6", "max_tokens": 4096,
                  "messages": [{"role": "user", "content": [media_block, {"type": "text", "text": prompt}]}]},
        )
    data = resp.json()
    if "error" in data:
        raise HTTPException(500, data["error"].get("message", "Claude error"))

    text = data.get("content", [{}])[0].get("text", "")
    m = re.search(r"\[[\s\S]*\]", text)
    if not m:
        raise HTTPException(500, f"Claude did not return valid JSON. Response: {text[:300]}")
    extracted = json.loads(m.group(0))

    # Match each node to Zabbix by IP
    results = []
    for node in extracted:
        ip   = (node.get("ip") or "").strip()
        name = (node.get("name") or "Unknown").strip()
        ntype = node.get("type", "switch")
        zbx_host = None

        if ip:
            ifaces = await call_zabbix("hostinterface.get", {
                "output": ["hostid", "ip"],
                "search": {"ip": ip}, "searchExact": True,
            })
            if isinstance(ifaces, list) and ifaces:
                hosts = await call_zabbix("host.get", {
                    "output": ["hostid", "host", "name"],
                    "hostids": [ifaces[0]["hostid"]],
                })
                zbx_host = hosts[0] if isinstance(hosts, list) and hosts else None

        results.append({
            "name":          name,
            "ip":            ip,
            "type":          ntype,
            "zabbix_host":   zbx_host,
            "zabbix_hostid": zbx_host["hostid"] if zbx_host else None,
            "matched":       zbx_host is not None,
        })

    return {
        "nodes":   results,
        "total":   len(results),
        "matched": sum(1 for r in results if r["matched"]),
        "skipped": sum(1 for r in results if not r["matched"]),
    }


class ImportNode(BaseModel):
    name: str
    ip: str = ""
    type: str = "switch"
    zabbix_hostid: Optional[str] = None


class CreateImportBody(BaseModel):
    name: str
    nodes: List[ImportNode]


@router.post("/create")
async def create_import_map(body: CreateImportBody, session: dict = Depends(require_operator)):
    if not body.name:
        raise HTTPException(400, "Map name required")
    if not body.nodes:
        raise HTTPException(400, "No matched nodes to create")

    layout_id = await execute(
        "INSERT INTO map_layouts (name, positions, is_default) VALUES (%s,'{}',0)",
        (body.name,),
    )
    cols  = max(1, math.ceil(math.sqrt(len(body.nodes) * 1.6)))
    x_gap = 220; y_gap = 160; x_off = 150; y_off = 120
    positions = {}

    for i, n in enumerate(body.nodes):
        col = i % cols
        row = i // cols
        x   = x_off + col * x_gap
        y   = y_off + row * y_gap
        nid = f"map_{layout_id}_{i + 1}"
        await execute(
            "INSERT INTO map_nodes (id, label, ip, type, x, y, layout_id, zabbix_host_id, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'ok') "
            "ON DUPLICATE KEY UPDATE label=VALUES(label),ip=VALUES(ip),type=VALUES(type),"
            "x=VALUES(x),y=VALUES(y),layout_id=VALUES(layout_id),zabbix_host_id=VALUES(zabbix_host_id)",
            (nid, n.name, n.ip, n.type, x, y, layout_id, n.zabbix_hostid),
        )
        positions[nid] = {"x": x, "y": y}

    await execute("UPDATE map_layouts SET positions=%s WHERE id=%s",
                  (json.dumps(positions), layout_id))
    return {"ok": True, "layout_id": layout_id, "nodes_created": len(body.nodes)}


@router.get("/key")
async def get_claude_key(session: dict = Depends(get_session)):
    cfg = await fetch_one("SELECT claude_key FROM zabbix_config LIMIT 1") or {}
    key = cfg.get("claude_key", "")
    masked = (key[:8] + "*" * 24 + key[-4:]) if len(key) > 12 else ""
    return {"has_key": bool(key), "masked": masked}


class SaveKeyBody(BaseModel):
    claude_key: str


@router.post("/key")
async def save_claude_key(body: SaveKeyBody, session: dict = Depends(require_admin)):
    if not body.claude_key.strip():
        raise HTTPException(400, "Key required")
    await execute("UPDATE zabbix_config SET claude_key=%s", (body.claude_key.strip(),))
    return {"ok": True}


@router.get("/aikeys")
async def get_ai_keys(session: dict = Depends(get_session)):
    cfg = await fetch_one(
        "SELECT claude_key,openai_key,gemini_key,grok_key,openrouter_key,"
        "groq_key,deepseek_key,mistral_key,together_key,ollama_url,"
        "claude_web_session,chatgpt_web_token,"
        "default_ai_provider,default_ai_model "
        "FROM zabbix_config LIMIT 1"
    ) or {}

    def mask(k):
        k = k or ""
        return (k[:8] + "*" * 16 + k[-4:]) if len(k) > 12 else ""

    def mask_session(k):
        """Mask a long session token showing first 12 and last 6 chars."""
        k = k or ""
        if isinstance(k, str) and k.startswith("{"):
            try:
                parsed = json.loads(k)
                if isinstance(parsed, dict):
                    k = str(parsed.get("access_token") or parsed.get("token") or "")
            except Exception:
                pass
        return (k[:12] + "*" * 16 + k[-6:]) if len(k) > 18 else ("*" * len(k) if k else "")

    ollama = cfg.get("ollama_url") or "http://localhost:11434"
    provider_catalog = [
        {"value": "claude", "label": "Claude (Anthropic)", "configured": bool(cfg.get("claude_key"))},
        {"value": "openai", "label": "OpenAI GPT/Codex", "configured": bool(cfg.get("openai_key"))},
        {"value": "gemini", "label": "Google Gemini", "configured": bool(cfg.get("gemini_key"))},
        {"value": "grok", "label": "xAI Grok", "configured": bool(cfg.get("grok_key"))},
        {"value": "openrouter", "label": "OpenRouter", "configured": bool(cfg.get("openrouter_key"))},
        {"value": "groq", "label": "Groq (free)", "configured": bool(cfg.get("groq_key"))},
        {"value": "deepseek", "label": "DeepSeek", "configured": bool(cfg.get("deepseek_key"))},
        {"value": "mistral", "label": "Mistral AI", "configured": bool(cfg.get("mistral_key"))},
        {"value": "together", "label": "Together.ai", "configured": bool(cfg.get("together_key"))},
        {"value": "ollama", "label": "Ollama (local)", "configured": True},
        {"value": "claude_web", "label": "Claude Web (subscription)", "configured": bool(cfg.get("claude_web_session"))},
        {"value": "chatgpt_web", "label": "ChatGPT Web (subscription)", "configured": bool(cfg.get("chatgpt_web_token"))},
    ]
    default_provider = cfg.get("default_ai_provider") or "claude"
    configured_providers = [item for item in provider_catalog if item["configured"]]
    if default_provider and not any(item["value"] == default_provider for item in configured_providers):
        fallback = next((item for item in provider_catalog if item["value"] == default_provider), None)
        if fallback:
            configured_providers.append(fallback)

    return {
        "claude":           {"has": bool(cfg.get("claude_key")),            "masked": mask(cfg.get("claude_key"))},
        "openai":           {"has": bool(cfg.get("openai_key")),            "masked": mask(cfg.get("openai_key"))},
        "gemini":           {"has": bool(cfg.get("gemini_key")),            "masked": mask(cfg.get("gemini_key"))},
        "grok":             {"has": bool(cfg.get("grok_key")),              "masked": mask(cfg.get("grok_key"))},
        "openrouter":       {"has": bool(cfg.get("openrouter_key")),        "masked": mask(cfg.get("openrouter_key"))},
        "groq":             {"has": bool(cfg.get("groq_key")),              "masked": mask(cfg.get("groq_key"))},
        "deepseek":         {"has": bool(cfg.get("deepseek_key")),          "masked": mask(cfg.get("deepseek_key"))},
        "mistral":          {"has": bool(cfg.get("mistral_key")),           "masked": mask(cfg.get("mistral_key"))},
        "together":         {"has": bool(cfg.get("together_key")),          "masked": mask(cfg.get("together_key"))},
        "ollama":           {"has": True, "url": ollama},   # always "configured" — just needs local Ollama running
        "claude_web":       {"has": bool(cfg.get("claude_web_session")),    "masked": mask_session(cfg.get("claude_web_session"))},
        "chatgpt_web":      {"has": bool(cfg.get("chatgpt_web_token")),     "masked": mask_session(cfg.get("chatgpt_web_token"))},
        "default_provider": default_provider,
        "default_model":    cfg.get("default_ai_model")    or "",
        "provider_catalog": provider_catalog,
        "configured_providers": configured_providers,
    }


class SaveAiKeysBody(BaseModel):
    claude_key:          Optional[str] = None
    openai_key:          Optional[str] = None
    gemini_key:          Optional[str] = None
    grok_key:            Optional[str] = None
    openrouter_key:      Optional[str] = None
    groq_key:            Optional[str] = None
    deepseek_key:        Optional[str] = None
    mistral_key:         Optional[str] = None
    together_key:        Optional[str] = None
    ollama_url:          Optional[str] = None
    claude_web_session:  Optional[str] = None
    chatgpt_web_token:   Optional[str] = None
    default_ai_provider: Optional[str] = None
    default_ai_model:    Optional[str] = None


@router.post("/aikeys")
async def save_ai_keys(body: SaveAiKeysBody, session: dict = Depends(require_admin)):
    fields = [
        "claude_key", "openai_key", "gemini_key", "grok_key", "openrouter_key",
        "groq_key", "deepseek_key", "mistral_key", "together_key", "ollama_url",
        "claude_web_session", "chatgpt_web_token",
        "default_ai_provider", "default_ai_model",
    ]
    sets, vals = [], []
    for f in fields:
        v = getattr(body, f, None)
        if v is not None:
            sets.append(f"{f}=%s")
            vals.append(v.strip() if isinstance(v, str) else v)
    if sets:
        await execute("UPDATE zabbix_config SET " + ",".join(sets), tuple(vals))
    return {"ok": True}


def _capture_html(status: str, title: str, subtitle: str, provider: str = "") -> str:
    """Return a self-closing HTML success/error page for the bookmarklet popup."""
    ok   = status == "success"
    icon = "✓" if ok else "✗"
    col  = "#00e676" if ok else "#ff4444"
    close_js = (
        "<script>"
        "setTimeout(function(){"
        "  if(window.opener){"
        "    window.opener.postMessage({type:'noc_web_session_saved',provider:'" + provider + "'},'*');"
        "  }"
        "  window.close();"
        "},1600);"
        "</script>"
    ) if ok else ""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>NOC Sentinel</title>
<style>
  body{{margin:0;background:#04060f;color:#e0eaff;display:flex;align-items:center;
        justify-content:center;flex-direction:column;gap:14px;height:100vh;
        font-family:'Segoe UI',system-ui,sans-serif;text-align:center;padding:20px;box-sizing:border-box}}
  .ic{{font-size:52px;line-height:1}}
  .tt{{font-size:17px;font-weight:700;color:{col}}}
  .st{{font-size:12px;color:#666;max-width:300px;line-height:1.5}}
  .br{{position:fixed;top:14px;font-size:10px;color:#333;letter-spacing:1.5px;text-transform:uppercase}}
</style></head>
<body>
  <div class="br">NOC SENTINEL</div>
  <div class="ic">{icon}</div>
  <div class="tt">{title}</div>
  <div class="st">{subtitle}</div>
  {close_js}
</body></html>"""


@router.get("/capture-web-session", response_class=HTMLResponse)
async def capture_web_session(
    provider: str = Query(...),
    token:    str = Query(...),
    bundle:   str | None = Query(None),
    session:  dict = Depends(get_session),   # must be logged in to NOC Sentinel
):
    """
    Called by the browser bookmarklet to automatically save a web subscription session.
    Returns an HTML page that closes itself and notifies the parent window.
    """
    if provider not in ("claude_web", "chatgpt_web"):
        return HTMLResponse(_capture_html("error", "Unknown provider", f"'{provider}' is not a valid provider."))

    token = (token or "").strip()
    if len(token) < 8:
        return HTMLResponse(_capture_html("error", "Token too short", "The captured token appears invalid. Try again."))

    if provider == "chatgpt_web" and bundle:
        try:
            decoded = base64.urlsafe_b64decode((bundle + "===").encode("utf-8"))
            parsed = json.loads(decoded.decode("utf-8"))
            if isinstance(parsed, dict):
                token = json.dumps(
                    {
                        "access_token": str(parsed.get("access_token") or token),
                        "device_id": str(parsed.get("device_id") or ""),
                        "cookies": str(parsed.get("cookies") or ""),
                    },
                    ensure_ascii=False,
                )
        except Exception:
            pass

    field  = "claude_web_session" if provider == "claude_web" else "chatgpt_web_token"
    label  = "Claude.ai" if provider == "claude_web" else "ChatGPT"
    await execute(f"UPDATE zabbix_config SET `{field}`=%s", (token,))

    return HTMLResponse(_capture_html(
        "success",
        f"{label} session saved!",
        "Your subscription is now active for AI Employees. This window will close.",
        provider,
    ))
