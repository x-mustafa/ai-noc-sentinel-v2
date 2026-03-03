import json
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Any

from app.deps import get_session
from app.database import fetch_one
from app.services.ai_provider import resolve_runtime_ai
from app.services.ai_stream import stream_ai

router = APIRouter()


class ChatBody(BaseModel):
    messages: List[Any] = []
    mode: str = "host"
    context_focus: str = "network"
    host_context: Optional[dict] = None
    network_stats: dict = {}
    context_data: dict = {}
    stream: bool = True


BASE_CONTEXT = """\
You are NOC Sentinel — the AI Network Intelligence System for Tabadul, Iraq's national payment processing infrastructure.

COMPANY: Tabadul (تبادل) — processes VISA, MasterCard, and Central Bank of Iraq (CBI) transactions for the Iraqi banking sector.

NETWORK ARCHITECTURE:
- External: VISA Network, MasterCard P14, CBI Switch, ISPs (ScopeSky, Passport-SS, Asia Local, Zain M2M)
- WAN Layer: ISP uplinks, P2P circuits
- Edge: Internet Switches, Core Switches (Cisco Catalyst 6800)
- Security: FortiGate 601E HA pair (Primary/Passive), Cisco Firepower 4150 HA (IPS/NGIPS)
- App Layer: Palo Alto PA-5250 HA pair (App-layer FW), F5 BIG-IP i7800 HA (Load Balancers)
- Servers: Payment apps, card processing, databases, HSMs
- DR: Active-Passive disaster recovery site

MONITORING PLATFORM: Zabbix 7.4.6

YOUR MISSION:
1. Be the intelligent eyes of the NOC team
2. Help engineers understand and resolve issues quickly
3. Recommend specific Zabbix templates, items, triggers, and thresholds
4. Speak in clear, technical English. Use bullet points. Be direct and actionable.
"""

SEV_LABELS = {0: "Not classified", 1: "Info", 2: "Warning", 3: "Average", 4: "High", 5: "Disaster"}


@router.post("")
async def chat(body: ChatBody, session: dict = Depends(get_session)):
    cfg = await fetch_one("SELECT * FROM zabbix_config LIMIT 1") or {}

    provider, model, api_key = resolve_runtime_ai(cfg)
    if not api_key:
        raise HTTPException(400, "AI API key not configured — go to Settings → AI Providers")

    # Build stats summary
    stats    = body.network_stats
    ctx_data = body.context_data
    total    = stats.get("total", "?")
    ok       = stats.get("ok", "?")
    prob     = stats.get("with_problems", "?")
    alarms   = stats.get("alarms", "?")
    unavail  = stats.get("unavailable", "?")

    rich_ctx = ""
    alarm_list    = ctx_data.get("alarm_list", [])
    problem_hosts = ctx_data.get("problem_hosts", [])
    map_nodes     = ctx_data.get("map_nodes", [])
    if alarm_list:
        lines = [f"  [{SEV_LABELS.get(a.get('severity', 0), '?')}] {a.get('name', 'Unknown')}"
                 for a in alarm_list]
        rich_ctx += f"\n\nACTIVE ALARMS ({len(alarm_list)}):\n" + "\n".join(lines)
    if problem_hosts:
        lines = [f"  - {h.get('host','?')}: {h.get('problems',0)} problem(s), "
                 f"worst severity {h.get('severity',0)}, available={h.get('available',0)}"
                 for h in problem_hosts]
        rich_ctx += "\n\nHOSTS WITH ACTIVE PROBLEMS:\n" + "\n".join(lines)
    if map_nodes:
        lines = [f"  - {n.get('label','?')} ({n.get('type','unknown')})" for n in map_nodes]
        rich_ctx += f"\n\nCURRENT MAP NODES ({len(map_nodes)}):\n" + "\n".join(lines)

    base = BASE_CONTEXT + (
        f"\nCURRENT NETWORK STATUS:\n"
        f"- Total monitored hosts: {total}\n"
        f"- Healthy: {ok} | With problems: {prob} | Unreachable: {unavail}\n"
        f"- Active alarms: {alarms}\n"
    )

    if body.mode == "host" and body.host_context:
        hc      = body.host_context
        hn      = hc.get("name", "Unknown")
        hip     = hc.get("ip", "Unknown")
        htyp    = hc.get("type", "switch")
        hrole   = hc.get("role", "")
        hst     = hc.get("status", "unknown")
        zbid    = hc.get("zabbix_id", "")
        probs   = hc.get("problems", [])
        ifaces  = hc.get("ifaces", [])
        probs_text  = "None" if not probs else "\n  - ".join(
            f"{p.get('name','Unknown')} (Sev:{p.get('severity',0)})" for p in probs)
        ifaces_text = "Not configured yet" if not ifaces else ", ".join(ifaces)
        system_prompt = base + f"""
── CURRENT HOST UNDER REVIEW ──
- Device: {hn}
- IP: {hip}
- Type: {htyp}
- Role: {hrole}
- Zabbix Host ID: {zbid}
- Current Status: {hst}
- Interfaces configured: {ifaces_text}
- Active Problems:
  - {probs_text}

YOUR BEHAVIOUR FOR THIS HOST:
Start by greeting the engineer and summarizing what you know about this device.
Then immediately begin your investigation by asking about role, interfaces,
critical services, recurring issues, and Zabbix templates.
After gathering info, provide specific Zabbix configuration recommendations."""
    else:
        system_prompt = base + rich_ctx + (
            f"\n\nMODE: General Network Intelligence. Context: {body.context_focus}\n\n"
            "You have live network data above. Use it to give specific, actionable answers. "
            "Reference actual host names and alarm names from the data provided."
        )

    if body.stream:
        async def sse_generator():
            async for chunk in stream_ai(provider, api_key, model, system_prompt,
                                          body.messages[-1]["content"] if body.messages else ""):
                event = chunk.get("event", "message")
                data  = chunk.get("data", "{}")
                if event == "message":
                    yield f"data: {data}\n\n"
                else:
                    yield f"event: {event}\ndata: {data}\n\n"

        return StreamingResponse(sse_generator(), media_type="text/event-stream",
                                  headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # Non-streaming fallback
    import httpx
    full_text = ""
    async for chunk in stream_ai(provider, api_key, model, system_prompt,
                                  body.messages[-1]["content"] if body.messages else ""):
        if chunk.get("event") == "done":
            break
        try:
            d = json.loads(chunk.get("data", "{}"))
            full_text += d.get("t", "")
        except Exception:
            pass
    return {"reply": full_text}
