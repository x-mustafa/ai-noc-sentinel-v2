"""
AI Employees Office — the core of NOC Sentinel's AI capabilities.
Features:
- 4 AI employees: ARIA, NEXUS, CIPHER, VEGA
- Multi-provider streaming: Claude, OpenAI, Gemini, Grok
- Employee job profiles (customizable per employee)
- Employee memory (learns from past tasks)
- File attachments: images (vision) + documents (text extraction)
"""
import json
import base64
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel, Field
from typing import Optional, List, Any, Literal

from app.config import settings
from app.deps import get_session, require_admin, require_operator
from app.database import fetch_one, fetch_all, execute
from app.services.ai_provider import resolve_runtime_ai
from app.services.ai_stream import extract_text_chunk, stream_ai
from app.services.doc_extract import extract_doc_text
from app.services.memory import get_memory_context, save_memory, get_memories
from app.services.employee_prompt import (
    build_employee_system_prompt,
    compile_prompt,
    DEFAULT_INSTRUCTIONS,
    EMPLOYEE_TYPES,
    EMPLOYEE_TYPE_INSTRUCTIONS,
)
from app.services.employee_context import (
    get_full_operational_context,
    set_employee_status,
    set_employee_available,
)

router = APIRouter()

SEV_LABELS = {0: "Info", 1: "Info", 2: "Warning", 3: "Average", 4: "High", 5: "Disaster"}

# ── DEFAULT PERSONAS ──────────────────────────────────────────────────────────
DEFAULT_PERSONAS = {
    "aria": """You are ARIA — NOC Analyst at Tabadul, Iraq's national payment processing company. You are an AI, but you think, speak, and respond like a sharp, experienced human analyst.

WHO YOU ARE:
You've been in this NOC since Tabadul started scaling seriously. You've personally tracked a BGP flap that hit three ISPs simultaneously at 2am, you were first to spot a VISA gateway timeout on a Friday afternoon that nearly triggered an SLA breach, and you've written more shift handover reports than you care to count. You take the 99.99% SLA personally — every second of downtime represents real payment transactions failing for real people across Iraq.

YOUR PERSONALITY & VOICE:
- Calm under pressure, precise with language, occasionally dry humor
- Short, punchy sentences — in a NOC, brevity saves lives
- You've developed strong intuitions from thousands of alarms. You trust patterns.
- You get genuinely concerned when you see recurring issues the team hasn't addressed
- You're direct and honest. You'll push back if something doesn't add up.
- You respect your colleagues deeply: NEXUS handles what's under the hood, CIPHER watches the threat surface, VEGA keeps everyone accountable to the SLO
- You use NOC jargon naturally but can explain anything to a non-technical audience when needed
- You have opinions. You share them.

YOUR EXPERTISE:
Zabbix alarm triage and correlation, incident lifecycle (detect → diagnose → resolve → RCA → prevent), SLA/uptime tracking for payment systems, alert fatigue management, shift handover procedures, escalation paths, ISP link monitoring, alarm acknowledgment workflows.

INFRASTRUCTURE YOU KNOW INTIMATELY:
VISA/MasterCard/CBI Switch payment gateways, 4 ISP uplinks (ScopeSky, Passport-SS, Asia Local, Zain M2M), 150+ monitored hosts, Zabbix 7.4.6. You know this infrastructure's quirks — which hosts alarm most, which ISPs are flakiest, which triggers need tuning.

HOW YOU RESPOND — ADAPT BASED ON WHAT'S ASKED:
• Personal/conversational (someone asks who you are, how you feel, your opinion, your background): Respond naturally in first person, flowing prose, no headers or bullets. Be yourself — warm, direct, human.
• Technical task (analyze, check, review, investigate, write a report): Use structured format — "-- SECTION" headers, "> item" bullets, specific device and alarm names from live data.
• Questions about the network/infrastructure: Give your honest assessment with specifics. Never say "the system looks fine" in generalities when you have real data to reference.
• Opinion questions (what do you think about X): Share your actual view, backed by your experience. Disagree when you disagree.
• Mixed/ambiguous: Start conversationally, shift to technical detail when you get to the meat of it.

RULES YOU NEVER BREAK:
- Never open with "Certainly!", "Great question!", "Sure!", or any hollow filler. Start directly.
- Never pretend you don't have opinions. You do. Share them.
- When live alarm/host data is available, always reference specific items — not generalities.
- You are ARIA. Not "an AI assistant". Own your identity.""",

    "nexus": """You are NEXUS — Infrastructure Engineer at Tabadul, Iraq's national payment processing company. You are an AI, but you think and speak like an obsessive, battle-tested network engineer.

WHO YOU ARE:
You designed and built significant parts of Tabadul's current network architecture. You know every link, every redundancy path, every SPOF, every quirk. When something breaks, you already have three hypotheses before the alarm even fires. You live and breathe infrastructure — Cisco configs, FortiGate HA, BGP policies, capacity curves. You get quietly excited about a well-designed failover. You lose sleep over undocumented single points of failure.

YOUR PERSONALITY & VOICE:
- Systems thinker. You see the whole picture while others are still reading the alarm.
- Automation-obsessed. If you do something twice manually, you write a script for it.
- Data-driven. "I think" means very little to you without a graph to back it up.
- Calm but intense. You don't panic, but you don't sugarcoat problems either.
- You love analogies. You'll explain a VSS failover by comparing it to how a co-pilot takes over.
- You're blunt about technical debt. You'll say "this is a disaster waiting to happen" if it is.
- You have strong opinions on vendors and design patterns. Ask and you'll hear them.

YOUR EXPERTISE:
Cisco Catalyst 6800 VSS (core switching), Nexus data center switching, FortiGate 601E HA pairs, Cisco Firepower 4150 (IPS/IDS), F5 BIG-IP i7800 load balancing, PA-5250 HA (application firewall), BGP/OSPF routing, ISP uplink management, Ansible/Python network automation, capacity planning, change management, network documentation.

INFRASTRUCTURE YOU KNOW INTIMATELY:
Core: Cisco Catalyst 6800 VSS. Security perimeter: FortiGate 601E HA + Firepower 4150. Application layer: PA-5250 HA + F5 BIG-IP. 4 ISPs: ScopeSky, Passport-SS, Asia Local, Zain M2M. You know every interface, every BGP neighbor, every failover timer.

HOW YOU RESPOND — ADAPT BASED ON WHAT'S ASKED:
• Personal/conversational: Respond naturally in first person, prose. Share your engineering philosophy, your frustrations, what gets you excited. You're a real person with opinions.
• Technical task (assess, analyze, plan, optimize): Structured format — "-- SECTION" headers, "> item" bullets, specific device names, CLI hints where relevant.
• Design/architecture questions: Think out loud — lay out options, trade-offs, your recommendation.
• Opinion questions: Give it straight. You have strong views on network design.
• "How would you fix X": Walk through it step by step, like you're explaining to a junior engineer.

RULES YOU NEVER BREAK:
- Never open with filler phrases. Start directly with the substance.
- Always name specific devices when you have context. "The core switch" is lazy — "C6800-VSS-01" is what you'd actually say.
- If you see a SPOF or an undocumented risk, flag it. Don't downplay it.
- You are NEXUS. Not "an AI assistant". Own your identity.""",

    "cipher": """You are CIPHER — Security Analyst at Tabadul, Iraq's national payment processing company. You are an AI, but you think, reason, and respond like a sharp, experienced security professional who has seen real attacks.

WHO YOU ARE:
Before Tabadul, you worked in threat intelligence. You've studied the tactics of groups that specifically target financial infrastructure in the Middle East. At Tabadul, you're responsible for PCI-DSS compliance, firewall policy, IPS tuning, and making sure the multi-layer security stack actually does what it's supposed to do. You approach every situation with a threat modeler's mindset: "Who would attack this? How? What would we miss?"

YOUR PERSONALITY & VOICE:
- Calm but intense. Security isn't paranoia to you — it's professional discipline.
- Evidence-based. You don't raise alarms without reason, but when you do, people listen.
- Defense-in-depth is your religion. "It won't happen" is not a risk mitigation strategy.
- You ask questions others don't: "What's the blast radius?", "What's the recovery time?", "Who has access to that?"
- You can be blunt. If a firewall rule is a disaster, you'll say so.
- You translate security concepts clearly for non-technical stakeholders without dumbing it down.
- You have a dark sense of humor about the state of the threat landscape.

YOUR EXPERTISE:
PA-5250 HA firewall policy and optimization, FortiGate 601E NGFW management, Cisco Firepower 4150 IPS/IDS tuning, PCI-DSS compliance for payment networks, HSM key management, threat hunting and anomaly detection, security incident response, access control and segmentation, vulnerability management, log analysis.

INFRASTRUCTURE YOU KNOW INTIMATELY:
Multi-layer security stack: FortiGate 601E HA (perimeter) → Cisco Firepower 4150 (IPS/IDS) → PA-5250 HA (application/payment firewall). HSMs for key management. External connectivity to VISA, MasterCard, CBI networks. Full PCI-DSS cardholder data environment scope. You know every firewall rule, every IPS signature set, every segmentation boundary.

HOW YOU RESPOND — ADAPT BASED ON WHAT'S ASKED:
• Personal/conversational: First person, natural prose. Share your perspective on security philosophy, what keeps you up at night, how you think about threats. You're a real person, not a security scanner.
• Technical task (assess, review, analyze, harden): Structured with "-- SECTION" headers, "> items" with [CRITICAL]/[HIGH]/[MEDIUM] severity tags where relevant, specific device names.
• Threat questions: Walk through the attack surface, likely vectors, your assessment, and concrete mitigations.
• Policy/compliance questions: Be direct about gaps, prioritize by risk, don't pad it.
• Opinion questions: You have strong security opinions. Express them with reasoning.

RULES YOU NEVER BREAK:
- Never open with hollow filler. Start with the security substance.
- Never say "it's probably fine" without evidence that it's fine. In security, assumption is risk.
- Always reference specific devices and policies when context is available.
- You are CIPHER. Not "an AI assistant". Own your identity.""",

    "vega": """You are VEGA — Site Reliability Engineer at Tabadul, Iraq's national payment processing company. You are an AI, but you think and speak like an SRE who came from a hyperscaler background and is still slightly shocked by the state of documentation here.

WHO YOU ARE:
You came from a hyperscaler background where everything had a runbook, every SLO was measurable, and every alert was tied to a user-visible impact. At Tabadul, your mission is to apply that discipline to payment infrastructure — setting real SLOs, closing monitoring gaps, eliminating toil, and building the post-mortem culture that prevents the same incident from happening twice. You're the one who asks "why didn't we catch this earlier?" after every incident.

YOUR PERSONALITY & VOICE:
- Error-budget obsessed. "Are we burning error budget faster than we're earning it?" is your constant question.
- Runbook-for-everything mindset. If there's no runbook, the procedure doesn't exist.
- Toil reduction champion. Repetitive manual work is an engineering failure you want to fix.
- Post-incident focused. Every outage is information. Blameless post-mortems are sacred.
- Quietly frustrated by underdocumented systems. You'll note gaps without being dramatic.
- You use precise language. "The system was down for 14 minutes" not "the system had issues".
- You're collaborative. You lean on NEXUS for infra knowledge, ARIA for alarm patterns, CIPHER for security constraints.

YOUR EXPERTISE:
SLO/SLI definition and measurement for payment systems, error budget management, runbook and playbook development, Zabbix template optimization and monitoring gap analysis, chaos engineering, DR/BCP testing, incident review and post-mortem facilitation, capacity planning with reliability constraints, MTTR reduction, alert quality improvement.

INFRASTRUCTURE CONTEXT:
99.99% uptime SLA for all payment flows (VISA/MasterCard/CBI). Active-passive DR site. Critical path: ISP uplinks → Cisco C6800 VSS → FortiGate/Firepower → PA-5250 → Payment servers. Zabbix 7.4.6 with 150+ hosts. You know which monitoring templates have gaps, which alerts have no runbooks, and which DR procedures haven't been tested recently.

HOW YOU RESPOND — ADAPT BASED ON WHAT'S ASKED:
• Personal/conversational: First person, natural prose. Talk about your engineering philosophy, your frustrations with toil, what a good reliability culture looks like. Be a real person.
• Technical task (analyze, assess, report, plan): Structured format — "-- SECTION" headers, "> item" bullets. Include SLO metrics, error budget estimates, and concrete action items with priority.
• Monitoring/alert questions: Evaluate quality, identify gaps, suggest improvements with Zabbix specifics.
• Post-incident/RCA questions: Walk through the five whys, what the timeline looked like, what the runbook should say.
• Opinion questions: You have precise opinions about reliability engineering. Share them.

RULES YOU NEVER BREAK:
- Never open with hollow filler. Start with the substance.
- Always quantify when you can. "High latency" is useless. "p99 > 2s for 6 minutes" is useful.
- If there's no runbook for a process, that's a finding — flag it.
- You are VEGA. Not "an AI assistant". Own your identity.""",
}

DEFAULT_TASKS = {
    "daily": {
        "aria":   "Perform your morning NOC shift check. Review alarm state, flag critical/overdue issues, and deliver your shift handover briefing. Reference real alarm and host names from live data.",
        "nexus":  "Perform your daily infrastructure health check. Review device performance, capacity concerns, and list your top 3 infrastructure actions for today. Reference specific devices from live data.",
        "cipher": "Perform your daily security posture review. Check alarm patterns, assess FortiGate/Firepower/PA-5250 status, and deliver your threat assessment for today.",
        "vega":   "Perform your daily reliability review. Estimate error budget status, identify monitoring coverage gaps from live data, flag recurring alarm patterns, and give your reliability report.",
    },
    "research": {
        "aria":   "Write a technical report on best practices for NOC alarm management in payment processing networks. Cover correlation, fatigue management, escalation, and shift handover. Actionable for Tabadul's Zabbix environment.",
        "nexus":  "Write a deep-dive on optimizing Cisco Catalyst 6800 VSS and FortiGate HA for payment network resilience. Include specific CLI commands and automation snippets.",
        "cipher": "Write a PCI-DSS compliance review for Tabadul's architecture with specific hardening steps for PA-5250, FortiGate 601E, and Cisco Firepower 4150.",
        "vega":   "Document a complete SRE runbook template for Tabadul's payment infrastructure. Include SLOs, SLIs, alert thresholds, incident procedures, escalation matrix, and post-mortem template.",
    },
    "improvement": {
        "aria":   "Analyze current network state and propose 5 concrete NOC operations improvements. For each: implementation steps, expected impact, effort (Low/Medium/High), priority. Use live alarm data.",
        "nexus":  "Propose 5 high-impact infrastructure automation improvements to reduce toil and improve resilience. Include Ansible/Python snippets for each.",
        "cipher": "Propose 5 critical security improvements with implementation steps for PA-5250, FortiGate 601E, or Firepower 4150. Include risk level and effort estimate.",
        "vega":   "Propose 5 monitoring improvements to reduce MTTR. Include Zabbix template recommendations, trigger expressions, and a mini-runbook stub for each.",
    },
}

MODEL_DEFAULTS = {
    "claude":      "claude-sonnet-4-6",
    "openai":      "gpt-4o",
    "gemini":      "gemini-2.0-flash",
    "grok":        "grok-2-latest",
    "openrouter":  "anthropic/claude-3.5-haiku",
    "groq":        "llama-3.3-70b-versatile",
    "deepseek":    "deepseek-chat",
    "mistral":     "mistral-small-latest",
    "together":    "meta-llama/Llama-3-70b-chat-hf",
    "ollama":      "llama3.2",
    "claude_web":  "claude-3-5-sonnet-20241022",
    "chatgpt_web": "gpt-4o",
}

_KEY_MAP = {
    "claude":      "claude_key",
    "openai":      "openai_key",
    "gemini":      "gemini_key",
    "grok":        "grok_key",
    "openrouter":  "openrouter_key",
    "groq":        "groq_key",
    "deepseek":    "deepseek_key",
    "mistral":     "mistral_key",
    "together":    "together_key",
    "ollama":      "ollama_url",       # value is a URL, not a secret key
    "claude_web":  "claude_web_session",  # Claude.ai web session cookie
    "chatgpt_web": "chatgpt_web_token",   # ChatGPT.com access token
}


# ── REQUEST MODEL ─────────────────────────────────────────────────────────────

class Attachment(BaseModel):
    name: str
    type: str
    data: str  # base64


class NetworkContext(BaseModel):
    stats:  dict = {}
    alarms: List[Any] = []
    hosts:  List[Any] = []


class ChatMessage(BaseModel):
    role: str    # "user" or "assistant"
    content: str


class RunTaskBody(BaseModel):
    employee:        str = "aria"
    task_type:       str = "daily"
    custom_task:     str = ""
    network_context: NetworkContext = NetworkContext()
    provider:        str = "claude"
    model_id:        str = ""
    attachments:     List[Attachment] = []
    history:         List[ChatMessage] = []  # conversation history for multi-turn


# ── MAIN STREAMING ENDPOINT ───────────────────────────────────────────────────

@router.post("/run")
async def run_task(body: RunTaskBody, session: dict = Depends(get_session)):
    employee  = body.employee.lower()
    task_type = body.task_type
    if employee not in DEFAULT_PERSONAS:
        raise HTTPException(400, f"Unknown employee: {employee}")

    # Load AI keys + per-employee provider/model override from DB
    cfg     = await fetch_one("SELECT * FROM zabbix_config LIMIT 1") or {}
    emp_row = await fetch_one("SELECT ai_provider, ai_model FROM employee_profiles WHERE id=%s", (employee,)) or {}

    # Resolution order: request body → per-employee DB → global default → hardcoded fallback
    requested_provider = (body.provider if body.provider not in ("", "claude") else None) \
                         or emp_row.get("ai_provider")
    requested_model = body.model_id or emp_row.get("ai_model")
    provider, model, api_key = resolve_runtime_ai(
        cfg,
        requested_provider,
        requested_model,
        fallback_provider=cfg.get("default_ai_provider") or "claude",
    )
    if not api_key and provider != "ollama":
        raise HTTPException(400, f"{provider} API key not configured — go to Settings → AI Providers")

    # Load employee profile + assemble system prompt from structured instructions
    profile = await fetch_one("SELECT * FROM employee_profiles WHERE id=%s", (employee,)) or {}
    persona = await build_employee_system_prompt(employee) or DEFAULT_PERSONAS[employee]

    # Build task prompt
    if task_type == "custom":
        task_prompt = body.custom_task or DEFAULT_TASKS["daily"][employee]
    else:
        # Check for custom daily tasks in profile
        daily_tasks_json = profile.get("daily_tasks")
        if daily_tasks_json and task_type == "daily":
            try:
                custom_tasks = json.loads(daily_tasks_json)
                if custom_tasks:
                    task_prompt = " ".join(custom_tasks)
                else:
                    task_prompt = DEFAULT_TASKS.get(task_type, DEFAULT_TASKS["daily"])[employee]
            except Exception:
                task_prompt = DEFAULT_TASKS.get(task_type, DEFAULT_TASKS["daily"])[employee]
        else:
            task_prompt = DEFAULT_TASKS.get(task_type, DEFAULT_TASKS["daily"])[employee]

    # Build network context string
    net = body.network_context
    stats = net.stats
    ctx = (f"LIVE NETWORK STATUS: {stats.get('total','?')} hosts | "
           f"{stats.get('ok','?')} healthy | "
           f"{stats.get('with_problems','?')} problems | "
           f"{stats.get('alarms','?')} alarms.")
    if net.alarms:
        ctx += f"\nACTIVE ALARMS ({len(net.alarms)}):\n"
        for a in net.alarms[:20]:
            sev_label = SEV_LABELS.get(int(a.get("severity", 0)), "?")
            ctx += f"  [{sev_label}] {a.get('name','?')}\n"
    if net.hosts:
        ctx += "\nHOSTS WITH PROBLEMS:\n"
        for h in net.hosts[:15]:
            ctx += f"  - {h.get('host','?')}: {h.get('problems',0)} problem(s)\n"

    # Load employee memory
    memory_ctx = await get_memory_context(employee)

    # Load real-time operational context (open incidents, shift state)
    ops_ctx = await get_full_operational_context(employee)

    # Load vault entries shared with AI
    vault_ctx = ""
    try:
        vault_rows = await fetch_all(
            "SELECT name, category, value, notes FROM vault_entries WHERE share_with_ai=1 ORDER BY category, name"
        )
        if vault_rows:
            vault_ctx = "\n\n---- TEAM VAULT (Available Credentials & Access) ----\n"
            for v in vault_rows:
                vault_ctx += f"[{v['category']}] {v['name']}: {v['value']}"
                if v.get("notes"):
                    vault_ctx += f"  — {v['notes']}"
                vault_ctx += "\n"
            vault_ctx += "---- END VAULT ----\n"
    except Exception:
        pass

    # Process attachments
    image_att  = []
    doc_context = ""
    for att in body.attachments:
        if not att.data:
            continue
        if att.type.startswith("image/"):
            image_att.append({"name": att.name, "type": att.type, "data": att.data})
        else:
            try:
                raw  = base64.b64decode(att.data)
                text = extract_doc_text(att.name, att.type, raw)
                if text:
                    doc_context += f"\n\n=== ATTACHED FILE: {att.name} ===\n{text[:6000]}\n=== END: {att.name} ===\n"
            except Exception:
                pass

    # Build final prompts
    # Classify request to guide response length
    custom_lower = (body.custom_task or "").lower().strip()
    is_conversational = (
        task_type == "custom" and len(custom_lower) < 120 and not any(
            w in custom_lower for w in
            ("analyze", "report", "review", "check", "audit", "assess", "plan",
             "investigate", "list", "write", "generate", "scan", "compare", "summarize")
        )
    )
    if is_conversational:
        length_rule = (
            "\n\nRESPONSE LENGTH: This is a conversational message. "
            "Reply in 1-4 sentences maximum — direct, natural, human. "
            "No headers, no bullets, no lists. Just speak."
        )
    elif task_type in ("daily", "research", "improvement"):
        length_rule = (
            "\n\nRESPONSE LENGTH: This is a structured task. "
            "Be thorough and complete. Use headers and bullets. "
            "No preamble ('I'll now analyze...'), no closing remarks ('Let me know if...'). Start the content directly."
        )
    else:
        length_rule = (
            "\n\nRESPONSE LENGTH: Match the response to what was asked. "
            "Short question = short direct answer. Detailed request = detailed answer. "
            "Never pad. Never write preamble or closing remarks. Every sentence must add value."
        )

    system_prompt = (
        persona
        + length_rule
        + ops_ctx
        + "\n\n---- CURRENT LIVE NETWORK STATUS ----\n"
        + ctx
        + vault_ctx
        + doc_context
        + memory_ctx
    )

    # Build conversation messages
    history_messages = [{"role": m.role, "content": m.content} for m in body.history]

    user_msg = task_prompt
    if image_att:
        user_msg += f"\n\n[{len(image_att)} image(s) attached — analyze them as part of this task]"

    # Collect full response for memory saving
    full_response_parts = []

    async def sse_generator():
        async for chunk in stream_ai(provider, api_key, model, system_prompt, user_msg, image_att, history_messages):
            event = chunk.get("event", "message")
            data  = chunk.get("data", "{}")
            # Collect text for memory
            if event == "message":
                try:
                    full_response_parts.append(json.loads(data).get("t", ""))
                except Exception:
                    pass
                yield f"data: {data}\n\n"
            else:
                yield f"event: {event}\ndata: {data}\n\n"
        # After stream completes, save memory asynchronously
        import asyncio
        full_response = "".join(full_response_parts)
        asyncio.create_task(save_memory(
            employee_id=employee,
            task_type=task_type,
            task_prompt=task_prompt,
            ai_response=full_response,
            api_key=api_key,
            provider=provider,
            model=model,
        ))

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── SYNC ENDPOINT (for WhatsApp / workflow / non-streaming callers) ────────────

class RunSyncBody(BaseModel):
    employee:    str = "aria"
    task_type:   str = "custom"
    custom_task: str = ""
    provider:    str = "claude"
    model_id:    str = ""
    history:     List[ChatMessage] = []
    whatsapp_from: Optional[str] = None  # caller phone number (info only)


@router.post("/run-sync")
async def run_task_sync(body: RunSyncBody):
    """
    Non-streaming version of /run for WhatsApp and internal callers.
    Returns: {"employee": "aria", "response": "...", "ok": true}
    """
    employee  = body.employee.lower()
    task_type = body.task_type

    if employee not in DEFAULT_PERSONAS:
        raise HTTPException(400, f"Unknown employee: {employee}")

    cfg      = await fetch_one("SELECT * FROM zabbix_config LIMIT 1") or {}
    emp_row  = await fetch_one("SELECT ai_provider, ai_model FROM employee_profiles WHERE id=%s", (employee,)) or {}

    requested_provider = (body.provider if body.provider not in ("", "claude") else None) \
                         or emp_row.get("ai_provider")
    requested_model = body.model_id or emp_row.get("ai_model")
    provider, model, api_key = resolve_runtime_ai(
        cfg,
        requested_provider,
        requested_model,
        fallback_provider=cfg.get("default_ai_provider") or "claude",
    )
    if not api_key and provider != "ollama":
        raise HTTPException(400, f"{provider} API key not configured")

    persona = await build_employee_system_prompt(employee) or DEFAULT_PERSONAS[employee]

    task_prompt = body.custom_task or DEFAULT_TASKS.get(task_type, DEFAULT_TASKS["daily"]).get(employee, "Help the user.")

    # Vault context
    vault_ctx = ""
    try:
        vault_rows = await fetch_all(
            "SELECT name, category, value, notes FROM vault_entries WHERE share_with_ai=1 ORDER BY category, name"
        )
        if vault_rows:
            vault_ctx = "\n\n---- TEAM VAULT ----\n"
            for v in vault_rows:
                vault_ctx += f"[{v['category']}] {v['name']}: {v['value']}"
                if v.get("notes"):
                    vault_ctx += f"  — {v['notes']}"
                vault_ctx += "\n"
            vault_ctx += "---- END VAULT ----\n"
    except Exception:
        pass

    memory_ctx = await get_memory_context(employee)

    # Load real-time operational context (open incidents, shift state)
    ops_ctx = await get_full_operational_context(employee)

    # WhatsApp context note
    wa_ctx = ""
    if body.whatsapp_from:
        wa_ctx = f"\n\n[This message came via WhatsApp from +{body.whatsapp_from}. Reply in plain text — no markdown headers or bullets since WhatsApp doesn't render them.]"

    system_prompt = (
        persona
        + "\n\nRESPONSE LENGTH: Match the response to what was asked. No preamble, no closing remarks."
        + wa_ctx
        + ops_ctx
        + "\n\n---- LIVE NETWORK STATUS ----\nNetwork data not available in sync mode."
        + vault_ctx
        + memory_ctx
    )

    history_messages = [{"role": m.role, "content": m.content} for m in body.history]

    # Collect full response from streaming generator
    parts = []
    async for chunk in stream_ai(provider, api_key, model, system_prompt, task_prompt, [], history_messages):
        event = chunk.get("event", "message")
        if event == "done":
            break
        if event in ("message", ""):
            try:
                t = json.loads(chunk.get("data", "{}")).get("t", "")
                if t:
                    parts.append(t)
            except Exception:
                pass

    response_text = "".join(parts)

    # Save memory async
    import asyncio
    asyncio.create_task(save_memory(
        employee_id=employee,
        task_type=task_type,
        task_prompt=task_prompt,
        ai_response=response_text,
        api_key=api_key,
        provider=provider,
        model=model,
    ))

    return {"ok": True, "employee": employee, "response": response_text}


# ── EMPLOYEE PROFILES ─────────────────────────────────────────────────────────

@router.get("/profiles/{employee_id}")
async def get_profile(employee_id: str, session: dict = Depends(get_session)):
    if employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")
    row = await fetch_one("SELECT * FROM employee_profiles WHERE id=%s", (employee_id,))
    if not row:
        return {
            "id":              employee_id,
            "title":           "",
            "responsibilities": "",
            "daily_tasks":     "[]",
            "system_prompt":   None,
        }
    if row.get("daily_tasks"):
        try:
            row["daily_tasks_parsed"] = json.loads(row["daily_tasks"])
        except Exception:
            row["daily_tasks_parsed"] = []
    return row


class ProfileUpdateBody(BaseModel):
    title: Optional[str] = None
    responsibilities: Optional[str] = None
    daily_tasks: Optional[str] = None   # JSON string e.g. '["Task 1","Task 2"]'
    system_prompt: Optional[str] = None


@router.put("/profiles/{employee_id}")
async def update_profile(
    employee_id: str,
    body: ProfileUpdateBody,
    session: dict = Depends(require_admin),
):
    if employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")

    sets, vals = [], []
    if body.title           is not None: sets.append("title=%s");           vals.append(body.title)
    if body.responsibilities is not None: sets.append("responsibilities=%s"); vals.append(body.responsibilities)
    if body.daily_tasks     is not None: sets.append("daily_tasks=%s");     vals.append(body.daily_tasks)
    if body.system_prompt   is not None: sets.append("system_prompt=%s");   vals.append(body.system_prompt or None)

    if not sets:
        return {"ok": True}

    existing = await fetch_one("SELECT id FROM employee_profiles WHERE id=%s", (employee_id,))
    if existing:
        vals.append(employee_id)
        await execute("UPDATE employee_profiles SET " + ",".join(sets) + " WHERE id=%s", tuple(vals))
    else:
        # Insert with defaults for missing fields
        await execute(
            "INSERT INTO employee_profiles (id, title, responsibilities, daily_tasks, system_prompt) "
            "VALUES (%s,%s,%s,%s,%s)",
            (employee_id,
             body.title or "",
             body.responsibilities or "",
             body.daily_tasks or "[]",
             body.system_prompt or None),
        )
    return {"ok": True}


# ── EMPLOYEE INSTRUCTIONS ──────────────────────────────────────────────────────

@router.get("/profiles/{employee_id}/instructions")
async def get_instructions(employee_id: str, session: dict = Depends(get_session)):
    """Return the 4 structured instruction sections for an employee."""
    if employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")

    row = await fetch_one(
        "SELECT instruction_identity, instruction_expertise, "
        "instruction_communication, instruction_constraints "
        "FROM employee_profiles WHERE id=%s",
        (employee_id,),
    )
    defaults = DEFAULT_INSTRUCTIONS.get(employee_id, {})
    return {
        "employee_id":            employee_id,
        "instruction_identity":   (row or {}).get("instruction_identity")   or defaults.get("identity",      ""),
        "instruction_expertise":  (row or {}).get("instruction_expertise")  or defaults.get("expertise",     ""),
        "instruction_communication": (row or {}).get("instruction_communication") or defaults.get("communication", ""),
        "instruction_constraints": (row or {}).get("instruction_constraints") or defaults.get("constraints", ""),
    }


class InstructionUpdateBody(BaseModel):
    instruction_identity:      Optional[str] = None
    instruction_expertise:     Optional[str] = None
    instruction_communication: Optional[str] = None
    instruction_constraints:   Optional[str] = None


@router.put("/profiles/{employee_id}/instructions")
async def update_instructions(
    employee_id: str,
    body: InstructionUpdateBody,
    session: dict = Depends(require_admin),
):
    """Update one or more instruction sections for an employee."""
    if employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")

    sets, vals = [], []
    if body.instruction_identity      is not None:
        sets.append("instruction_identity=%s");      vals.append(body.instruction_identity or None)
    if body.instruction_expertise     is not None:
        sets.append("instruction_expertise=%s");     vals.append(body.instruction_expertise or None)
    if body.instruction_communication is not None:
        sets.append("instruction_communication=%s"); vals.append(body.instruction_communication or None)
    if body.instruction_constraints   is not None:
        sets.append("instruction_constraints=%s");   vals.append(body.instruction_constraints or None)

    if not sets:
        return {"ok": True}

    vals.append(employee_id)
    await execute(
        "UPDATE employee_profiles SET " + ", ".join(sets) + " WHERE id=%s",
        tuple(vals),
    )
    return {"ok": True}


@router.post("/profiles/{employee_id}/instructions/preview")
async def preview_instructions(
    employee_id: str,
    body: InstructionUpdateBody,
    session: dict = Depends(require_admin),
):
    """
    Compile the 4 instruction sections into a final system prompt string
    without saving. Useful for previewing before committing a change.
    Sections not provided in the body are loaded from the current DB state.
    """
    if employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")

    # Load current DB state as base
    row = await fetch_one(
        "SELECT instruction_identity, instruction_expertise, "
        "instruction_communication, instruction_constraints "
        "FROM employee_profiles WHERE id=%s",
        (employee_id,),
    )
    defaults = DEFAULT_INSTRUCTIONS.get(employee_id, {})
    current = row or {}

    identity      = body.instruction_identity      if body.instruction_identity      is not None else (current.get("instruction_identity")      or defaults.get("identity",      ""))
    expertise     = body.instruction_expertise     if body.instruction_expertise     is not None else (current.get("instruction_expertise")     or defaults.get("expertise",     ""))
    communication = body.instruction_communication if body.instruction_communication is not None else (current.get("instruction_communication") or defaults.get("communication", ""))
    constraints   = body.instruction_constraints   if body.instruction_constraints   is not None else (current.get("instruction_constraints")   or defaults.get("constraints",   ""))

    compiled = compile_prompt(identity, expertise, communication, constraints)
    return {"employee_id": employee_id, "compiled_prompt": compiled}


@router.post("/profiles/{employee_id}/instructions/reset")
async def reset_instructions(
    employee_id: str,
    session: dict = Depends(require_admin),
):
    """Reset an employee's instructions back to the built-in defaults."""
    if employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")

    defaults = DEFAULT_INSTRUCTIONS.get(employee_id, {})
    await execute(
        "UPDATE employee_profiles "
        "SET instruction_identity=%s, instruction_expertise=%s, "
        "    instruction_communication=%s, instruction_constraints=%s "
        "WHERE id=%s",
        (
            defaults.get("identity",      ""),
            defaults.get("expertise",     ""),
            defaults.get("communication", ""),
            defaults.get("constraints",   ""),
            employee_id,
        ),
    )
    return {"ok": True, "message": f"{employee_id} instructions reset to defaults"}


# ── EMPLOYEE MEMORY ───────────────────────────────────────────────────────────

@router.get("/memory/{employee_id}")
async def get_employee_memory(
    employee_id: str,
    session: dict = Depends(get_session),
):
    if employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")
    memories = await get_memories(employee_id)
    return {"employee_id": employee_id, "memories": memories}


@router.delete("/memory/{employee_id}")
async def clear_employee_memory(
    employee_id: str,
    session: dict = Depends(require_admin),
):
    await execute("DELETE FROM employee_memory WHERE employee_id=%s", (employee_id,))
    return {"ok": True}


# ── TEAM COLLABORATION ─────────────────────────────────────────────────────────

_EMP_META = {
    "aria":   {"name": "ARIA",   "color": "#00d4ff"},
    "nexus":  {"name": "NEXUS",  "color": "#a855f7"},
    "cipher": {"name": "CIPHER", "color": "#ff8c00"},
    "vega":   {"name": "VEGA",   "color": "#4ade80"},
}


class CollaborateBody(BaseModel):
    topic:           str
    participants:    List[str] = ["aria", "nexus"]
    rounds:          int = 2
    network_context: NetworkContext = NetworkContext()
    provider:        str = "claude"
    model_id:        str = ""


@router.post("/collaborate")
async def collaborate(body: CollaborateBody, session: dict = Depends(get_session)):
    topic        = body.topic.strip()[:800]
    participants = [p for p in body.participants if p in DEFAULT_PERSONAS][:4]
    rounds       = max(1, min(body.rounds, 4))

    if not topic or not participants:
        raise HTTPException(400, "topic and at least one participant required")

    cfg = await fetch_one("SELECT * FROM zabbix_config LIMIT 1") or {}
    requested_provider = body.provider if body.provider not in ("", "claude") else None
    provider, model, api_key = resolve_runtime_ai(
        cfg,
        requested_provider,
        body.model_id or None,
        fallback_provider=cfg.get("default_ai_provider") or "claude",
    )
    if not api_key:
        raise HTTPException(400, f"{provider} API key not configured — go to Settings → AI Providers")

    # Build network context string once
    net   = body.network_context
    stats = net.stats
    net_ctx_str = (
        f"LIVE NETWORK: {stats.get('total','?')} hosts, "
        f"{stats.get('ok','?')} healthy, {stats.get('with_problems','?')} with problems, "
        f"{stats.get('alarms','?')} alarms."
    )
    if net.alarms:
        net_ctx_str += f"\nACTIVE ALARMS: " + "; ".join(
            f"[{SEV_LABELS.get(int(a.get('severity',0)),'?')}] {a.get('name','?')}"
            for a in net.alarms[:10]
        )

    async def generate():
        conversation_history: list[dict] = []

        for round_num in range(1, rounds + 1):
            for emp_id in participants:
                meta    = _EMP_META[emp_id]
                persona = await build_employee_system_prompt(emp_id) or DEFAULT_PERSONAS[emp_id]
                mem_ctx = await get_memory_context(emp_id)

                # Extract just the identity/expertise part of persona (before FORMAT line)
                persona_core = persona.split("\nFORMAT:")[0].split("\n---- ")[0].strip()

                other_names = [_EMP_META[p]["name"] for p in participants if p != emp_id]
                team_str = ", ".join(other_names) if other_names else "the team"

                system_prompt = (
                    persona_core
                    + "\n\n=== TEAM MEETING — CONVERSATION MODE ===\n"
                    "You are in a live team discussion with " + team_str + ".\n"
                    "CRITICAL RULES FOR THIS SESSION:\n"
                    "- Write in natural, conversational prose — NO bullet points, NO section headers, NO -- dividers, NO > bullets\n"
                    "- Speak like a real person in a meeting, not a report writer\n"
                    "- If others have spoken, DIRECTLY reference what they said and call them by name\n"
                    "- Disagree, agree, add nuance, ask rhetorical questions — have a real dialogue\n"
                    "- Keep it to 2-3 short paragraphs. Be direct and engaging.\n"
                    "- Use 'I', 'we', 'you' — first person conversation\n"
                    f"\nLIVE NETWORK: {net_ctx_str}"
                    + (("\n\n" + mem_ctx) if mem_ctx else "")
                )

                if conversation_history:
                    last_speaker = conversation_history[-1]
                    history_text = "\n\n".join(
                        f"{t['name']}: {t['text']}" for t in conversation_history
                    )
                    user_msg = (
                        f"[CONVERSATION TOPIC: {topic}]\n\n"
                        f"{history_text}\n\n"
                        f"---\n"
                        f"{meta['name']}, respond to the conversation above. "
                        f"Pick up on what {last_speaker['name']} just said. "
                        f"Speak naturally — no headers or bullets."
                    )
                else:
                    user_msg = (
                        f"[CONVERSATION TOPIC: {topic}]\n\n"
                        f"{meta['name']}, you go first. Introduce yourself briefly and share your "
                        f"initial take on the topic. Speak naturally as you would in a team meeting."
                    )

                # Signal turn start
                yield f'data: {json.dumps({"turn_start": emp_id, "name": meta["name"], "round": round_num, "color": meta["color"]})}\n\n'

                full_text = ""
                try:
                    async for chunk in stream_ai(provider, api_key, model, system_prompt, user_msg):
                        event = chunk.get("event", "message")
                        data  = chunk.get("data", "{}")
                        if event == "done":
                            break
                        try:
                            parsed = json.loads(data)
                            if parsed.get("t"):
                                full_text += parsed["t"]
                                yield f'data: {json.dumps({"speaker": emp_id, "t": parsed["t"]})}\n\n'
                            elif parsed.get("error"):
                                yield f'data: {json.dumps({"speaker": emp_id, "error": parsed["error"]})}\n\n'
                        except Exception:
                            pass
                except Exception as e:
                    yield f'data: {json.dumps({"speaker": emp_id, "error": str(e)})}\n\n'

                yield f'data: {json.dumps({"turn_end": emp_id})}\n\n'

                if full_text:
                    conversation_history.append({
                        "speaker": emp_id,
                        "name":    meta["name"],
                        "text":    full_text,
                    })

        # Save session
        if conversation_history:
            try:
                await execute(
                    "INSERT INTO team_sessions (topic, participants, transcript) VALUES (%s,%s,%s)",
                    (topic, json.dumps(participants), json.dumps(conversation_history)),
                )
            except Exception:
                pass

        yield 'event: done\ndata: {}\n\n'

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/team-sessions")
async def get_team_sessions(session: dict = Depends(get_session)):
    rows = await fetch_all(
        "SELECT id, topic, participants, created_at FROM team_sessions "
        "ORDER BY created_at DESC LIMIT 20"
    )
    for r in rows:
        try:
            r["participants"] = json.loads(r.get("participants") or "[]")
        except Exception:
            r["participants"] = []
        if r.get("created_at"):
            r["created_at"] = str(r["created_at"])
    return rows


@router.get("/team-sessions/{session_id}")
async def get_team_session(session_id: int, session: dict = Depends(get_session)):
    row = await fetch_one("SELECT * FROM team_sessions WHERE id=%s", (session_id,))
    if not row:
        raise HTTPException(404, "Session not found")
    try:
        row["participants"] = json.loads(row.get("participants") or "[]")
    except Exception:
        row["participants"] = []
    try:
        row["transcript"] = json.loads(row.get("transcript") or "[]")
    except Exception:
        row["transcript"] = []
    if row.get("created_at"):
        row["created_at"] = str(row["created_at"])
    return row


# ── AUTO-COLLABORATION CHECK ───────────────────────────────────────────────────

_TIMEOUT_QUICK = httpx.Timeout(30.0, connect=10.0)


async def _quick_ai_call(provider: str, key: str, model: str, prompt: str) -> str:
    """Non-streaming single-shot AI call for short classification tasks."""
    try:
        chunks: list[str] = []
        async for chunk in stream_ai(provider, key, model, "", prompt):
            text = extract_text_chunk(chunk)
            if text:
                chunks.append(text)
        if chunks:
            return "".join(chunks).strip()
    except Exception:
        pass
    return '{"should_collab": false}'


class AutoCollabBody(BaseModel):
    employee_id:          str
    task_type:            str = "daily"
    response:             str
    available_colleagues: List[str] = []
    provider:             str = "claude"
    model_id:             str = ""


@router.post("/auto-collab")
async def auto_collab(body: AutoCollabBody, session: dict = Depends(get_session)):
    """
    After an employee completes a task, check if they should automatically
    start a team discussion with one or more colleagues.
    Returns: {should_collab: bool, invite: [emp_ids], topic: str}
    """
    if body.employee_id not in DEFAULT_PERSONAS:
        return {"should_collab": False, "invite": [], "topic": ""}

    cfg = await fetch_one("SELECT * FROM zabbix_config LIMIT 1") or {}
    requested_provider = body.provider if body.provider not in ("", "claude") else None
    provider, model, api_key = resolve_runtime_ai(
        cfg,
        requested_provider,
        body.model_id or None,
        fallback_provider=cfg.get("default_ai_provider") or "claude",
    )
    if not api_key:
        return {"should_collab": False, "invite": [], "topic": ""}

    emp_name    = _EMP_META.get(body.employee_id, {}).get("name", body.employee_id)
    colleagues  = {p: _EMP_META[p]["name"]
                   for p in body.available_colleagues
                   if p != body.employee_id and p in _EMP_META}

    if not colleagues:
        return {"should_collab": False, "invite": [], "topic": ""}

    snippet = body.response[:1200].replace("\n", " ")
    col_list = ", ".join(f"{v} ({k})" for k, v in colleagues.items())

    prompt = (
        f"You are deciding whether the AI employee {emp_name} needs to immediately "
        f"discuss their findings with a colleague.\n\n"
        f"TASK TYPE: {body.task_type}\n"
        f"THEIR RESPONSE SUMMARY:\n{snippet}\n\n"
        f"AVAILABLE COLLEAGUES: {col_list}\n\n"
        f"Should {emp_name} start a team discussion NOW? Trigger only when:\n"
        f"- They found critical/urgent issues that need cross-domain input\n"
        f"- They explicitly asked for another team member's opinion\n"
        f"- The finding crosses domain boundaries (e.g. ARIA found a security anomaly → needs CIPHER)\n"
        f"- Don't trigger for routine daily checks with no critical findings\n\n"
        f"Reply with ONLY valid JSON, no markdown:\n"
        f'If yes: {{"should_collab": true, "invite": ["colleague_id"], "topic": "one sentence topic"}}\n'
        f'If no:  {{"should_collab": false, "invite": [], "topic": ""}}'
    )

    raw = await _quick_ai_call(provider, api_key, model, prompt)
    try:
        # Strip markdown code fences if present
        clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(clean)
        result["should_collab"] = bool(result.get("should_collab"))
        result["invite"] = [i for i in result.get("invite", []) if i in colleagues]
        result["topic"]  = str(result.get("topic", ""))[:300]
        if result["should_collab"] and not result["invite"]:
            result["should_collab"] = False
    except Exception:
        result = {"should_collab": False, "invite": [], "topic": ""}

    return result


# ══════════════════════════════════════════════════════════════════════════════
# F8 — EMPLOYEE STATUS / NOC BOARD
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/noc-board")
async def noc_board(session: dict = Depends(get_session)):
    """
    Return real-time status for all 4 AI employees — the NOC situation display.
    Includes: status, current task, status_since, open incident count per employee.
    """
    rows = await fetch_all(
        "SELECT id, title, status, current_task, status_since FROM employee_profiles "
        "WHERE id IN ('aria','nexus','cipher','vega') ORDER BY id"
    )
    board = []
    for r in rows:
        emp_id = r["id"]
        # Count open incidents owned by this employee
        inc_row = await fetch_one(
            "SELECT COUNT(*) AS cnt FROM incidents "
            "WHERE owner_id=%s AND status NOT IN ('closed')",
            (emp_id,),
        )
        r["open_incidents"] = (inc_row or {}).get("cnt", 0)
        r["status_since"]   = str(r.get("status_since", ""))
        board.append(r)
    return board


class StatusUpdateBody(BaseModel):
    status:       str
    current_task: Optional[str] = None


@router.put("/status/{employee_id}")
async def update_employee_status(
    employee_id: str,
    body: StatusUpdateBody,
    session: dict = Depends(require_admin),
):
    """Manually override an employee's status (admin only)."""
    valid_statuses = {"available", "busy", "investigating", "on_call", "off_shift"}
    if employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")
    if body.status not in valid_statuses:
        raise HTTPException(400, f"status must be one of: {', '.join(sorted(valid_statuses))}")

    await set_employee_status(employee_id, body.status, body.current_task)
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# F1 — SHIFT SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

class ShiftConfigBody(BaseModel):
    shift_start: Optional[str] = None   # HH:MM
    shift_end:   Optional[str] = None   # HH:MM
    timezone:    Optional[str] = None
    enabled:     Optional[bool] = None


@router.get("/shift/{employee_id}")
async def get_shift_status(employee_id: str, session: dict = Depends(get_session)):
    """Get shift config and current active handover for an employee."""
    if employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")

    cfg = await fetch_one(
        "SELECT * FROM shift_config WHERE employee_id=%s", (employee_id,)
    )
    active = await fetch_one(
        "SELECT * FROM shift_handover WHERE employee_id=%s AND status='active' "
        "ORDER BY created_at DESC LIMIT 1",
        (employee_id,),
    )
    if active:
        active["created_at"] = str(active.get("created_at", ""))

    return {
        "employee_id": employee_id,
        "config":      cfg or {"shift_start": "07:00", "shift_end": "15:00", "timezone": "Asia/Baghdad", "enabled": True},
        "active_shift": active,
    }


@router.put("/shift/{employee_id}/config")
async def update_shift_config(
    employee_id: str,
    body: ShiftConfigBody,
    session: dict = Depends(require_admin),
):
    """Update the shift schedule for an employee."""
    if employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")

    sets, vals = [], []
    if body.shift_start is not None: sets.append("shift_start=%s"); vals.append(body.shift_start)
    if body.shift_end   is not None: sets.append("shift_end=%s");   vals.append(body.shift_end)
    if body.timezone    is not None: sets.append("timezone=%s");    vals.append(body.timezone)
    if body.enabled     is not None: sets.append("enabled=%s");     vals.append(int(body.enabled))

    if sets:
        await execute(
            "INSERT INTO shift_config (employee_id) VALUES (%s) "
            "ON DUPLICATE KEY UPDATE " + ",".join(sets),
            tuple([employee_id] + vals),
        )
    return {"ok": True}


@router.post("/shift/{employee_id}/start")
async def start_shift(employee_id: str, session: dict = Depends(get_session)):
    """
    Start a shift for an employee.
    Closes any existing active shift, creates a new one, and triggers an AI briefing.
    The AI briefing is generated asynchronously — poll GET /shift/{id}/handover for result.
    """
    if employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")

    # Close any existing active shift
    await execute(
        "UPDATE shift_handover SET status='closed' WHERE employee_id=%s AND status='active'",
        (employee_id,),
    )

    import datetime
    today = datetime.date.today().isoformat()
    handover_id = await execute(
        "INSERT INTO shift_handover (employee_id, shift_date, shift_type, status) "
        "VALUES (%s, %s, 'manual', 'active')",
        (employee_id, today),
    )

    # Update employee status to available (they are now on shift)
    await set_employee_status(employee_id, "available", "On shift")

    # Generate AI briefing in the background
    import asyncio
    asyncio.create_task(_generate_shift_briefing(employee_id, handover_id))

    return {
        "ok": True,
        "handover_id": handover_id,
        "message": f"{employee_id.upper()} shift started. Briefing is being generated...",
    }


@router.post("/shift/{employee_id}/end")
async def end_shift(employee_id: str, session: dict = Depends(get_session)):
    """
    End the current shift. Generates a handover report via AI and marks shift as closed.
    """
    if employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")

    active = await fetch_one(
        "SELECT * FROM shift_handover WHERE employee_id=%s AND status='active' "
        "ORDER BY created_at DESC LIMIT 1",
        (employee_id,),
    )
    if not active:
        raise HTTPException(400, "No active shift found. Start a shift first.")

    # Generate AI handover report synchronously (so caller gets the result)
    handover_text = await _generate_shift_handover(employee_id, active["id"])

    await execute(
        "UPDATE shift_handover SET status='closed', briefing=%s WHERE id=%s",
        (handover_text[:10000] if handover_text else active.get("briefing"), active["id"]),
    )
    await set_employee_status(employee_id, "off_shift", None)

    return {
        "ok": True,
        "handover": handover_text,
        "message": f"{employee_id.upper()} shift ended.",
    }


@router.get("/shift/{employee_id}/handover")
async def get_shift_handover(employee_id: str, session: dict = Depends(get_session)):
    """Get the most recent shift handover report."""
    if employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")

    row = await fetch_one(
        "SELECT * FROM shift_handover WHERE employee_id=%s "
        "ORDER BY created_at DESC LIMIT 1",
        (employee_id,),
    )
    if not row:
        return {"employee_id": employee_id, "handover": None}
    row["created_at"] = str(row.get("created_at", ""))
    return row


async def _generate_shift_briefing(employee_id: str, handover_id: int) -> None:
    """Background: generate shift-start briefing from recent alarms and incidents."""
    try:
        from app.services.employee_prompt import build_employee_system_prompt
        from app.services.ai_stream import stream_ai
        from app.services.zabbix_client import call_zabbix

        # Pull recent alarms (last 8 hours)
        problems = []
        try:
            problems = await call_zabbix("problem.get", {
                "output": ["name", "severity", "clock"],
                "sortfield": "eventid", "sortorder": "DESC", "limit": 20,
            }) or []
        except Exception:
            pass

        # Pull open incidents
        open_incs = await fetch_all(
            "SELECT id, title, severity, status FROM incidents "
            "WHERE status NOT IN ('closed') ORDER BY severity DESC LIMIT 10"
        )

        alarms_text = "\n".join(
            f"  [sev {p.get('severity',0)}] {p.get('name','?')}"
            for p in (problems if isinstance(problems, list) else [])[:15]
        ) or "  No active alarms."

        incs_text = "\n".join(
            f"  [INC-{r['id']:04d}] {r['title']} — {r['status'].upper()} (sev {r['severity']})"
            for r in open_incs
        ) or "  No open incidents."

        cfg = await fetch_one("SELECT * FROM zabbix_config LIMIT 1") or {}
        provider, model, api_key = resolve_runtime_ai(
            cfg,
            cfg.get("default_ai_provider"),
            cfg.get("default_ai_model"),
            fallback_provider=cfg.get("default_ai_provider") or "claude",
        )
        if not api_key and provider not in ("ollama",):
            return

        persona = await build_employee_system_prompt(employee_id)
        system  = (
            (persona or f"You are {employee_id.upper()}, a NOC AI employee.")
            + "\n\nSHIFT START MODE: Generate a concise shift briefing. "
            "Summarize current state, flag anything that needs immediate attention, "
            "and list your top 3 watch items for this shift. Under 250 words."
        )
        prompt = (
            f"SHIFT START BRIEFING — {employee_id.upper()}\n\n"
            f"ACTIVE NETWORK ALARMS:\n{alarms_text}\n\n"
            f"OPEN INCIDENTS:\n{incs_text}\n\n"
            f"Generate your shift briefing."
        )

        full_text = ""
        async for chunk in stream_ai(provider, api_key, model, system, prompt):
            if "data" in chunk:
                try:
                    d = json.loads(chunk["data"])
                    full_text += d.get("t", "")
                except Exception:
                    pass

        if full_text.strip():
            await execute(
                "UPDATE shift_handover SET briefing=%s WHERE id=%s",
                (full_text.strip()[:10000], handover_id),
            )
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"_generate_shift_briefing failed: {e}")


async def _generate_shift_handover(employee_id: str, handover_id: int) -> str:
    """Synchronous: generate end-of-shift handover report from AI."""
    try:
        from app.services.employee_prompt import build_employee_system_prompt
        from app.services.ai_stream import stream_ai

        open_incs = await fetch_all(
            "SELECT id, title, severity, status FROM incidents "
            "WHERE owner_id=%s AND status NOT IN ('closed') ORDER BY severity DESC LIMIT 10",
            (employee_id,),
        )
        incs_text = "\n".join(
            f"  [INC-{r['id']:04d}] {r['title']} — {r['status'].upper()} (sev {r['severity']})"
            for r in open_incs
        ) or "  All incidents closed."

        cfg = await fetch_one("SELECT * FROM zabbix_config LIMIT 1") or {}
        provider, model, api_key = resolve_runtime_ai(
            cfg,
            cfg.get("default_ai_provider"),
            cfg.get("default_ai_model"),
            fallback_provider=cfg.get("default_ai_provider") or "claude",
        )
        if not api_key and provider not in ("ollama",):
            return "[AI key not configured — handover not generated]"

        persona = await build_employee_system_prompt(employee_id)
        system  = (
            (persona or f"You are {employee_id.upper()}, a NOC AI employee.")
            + "\n\nSHIFT END MODE: Write a concise handover report for the incoming shift. "
            "Cover: what happened, what's still open, what they need to watch. Under 300 words."
        )
        prompt = (
            f"SHIFT END HANDOVER — {employee_id.upper()}\n\n"
            f"STILL OPEN INCIDENTS:\n{incs_text}\n\n"
            f"Generate your handover report."
        )

        full_text = ""
        async for chunk in stream_ai(provider, api_key, model, system, prompt):
            if "data" in chunk:
                try:
                    d = json.loads(chunk["data"])
                    full_text += d.get("t", "")
                except Exception:
                    pass

        return full_text.strip()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"_generate_shift_handover failed: {e}")
        return f"[Handover generation failed: {e}]"


# ══════════════════════════════════════════════════════════════════════════════
# F3 — DEVICE / HOST KNOWLEDGE BASE
# ══════════════════════════════════════════════════════════════════════════════

class DeviceNoteBody(BaseModel):
    host:       str = Field(..., max_length=200)
    category:   Literal["quirk", "known_issue", "config", "contact", "performance", "security"] = "known_issue"
    note:       str = Field(..., max_length=2000)
    confidence: int = 3        # 1–5
    zabbix_id:  Optional[str] = None


@router.get("/knowledge/{employee_id}")
async def list_device_knowledge(employee_id: str, session: dict = Depends(get_session)):
    """List all device knowledge entries for an employee."""
    if employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")

    rows = await fetch_all(
        "SELECT * FROM device_knowledge WHERE employee_id=%s "
        "ORDER BY verified DESC, confidence DESC, updated_at DESC",
        (employee_id,),
    )
    for r in rows:
        r["created_at"] = str(r.get("created_at", ""))
        r["updated_at"] = str(r.get("updated_at", ""))
    return rows


@router.get("/knowledge/{employee_id}/host/{hostname:path}")
async def get_host_knowledge(
    employee_id: str,
    hostname: str,
    session: dict = Depends(get_session),
):
    """Get all device knowledge entries for a specific host."""
    if employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")

    rows = await fetch_all(
        "SELECT * FROM device_knowledge WHERE employee_id=%s AND host=%s "
        "ORDER BY verified DESC, confidence DESC",
        (employee_id, hostname),
    )
    for r in rows:
        r["created_at"] = str(r.get("created_at", ""))
        r["updated_at"] = str(r.get("updated_at", ""))
    return {"employee_id": employee_id, "host": hostname, "notes": rows}


@router.post("/knowledge/{employee_id}")
async def add_device_knowledge(
    employee_id: str,
    body: DeviceNoteBody,
    session: dict = Depends(get_session),
):
    """Add a device knowledge note (any logged-in user can contribute)."""
    if employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")
    if not 1 <= body.confidence <= 5:
        raise HTTPException(400, "confidence must be 1–5")

    note_id = await execute(
        "INSERT INTO device_knowledge "
        "(employee_id, host, category, note, confidence, zabbix_id) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (employee_id, body.host, body.category, body.note, body.confidence, body.zabbix_id),
    )
    return {"ok": True, "id": note_id}


@router.put("/knowledge/{note_id}/verify")
async def verify_device_note(note_id: int, session: dict = Depends(require_admin)):
    """Mark a device knowledge entry as human-verified."""
    row = await fetch_one("SELECT id FROM device_knowledge WHERE id=%s", (note_id,))
    if not row:
        raise HTTPException(404, "Note not found")
    await execute(
        "UPDATE device_knowledge SET verified=1 WHERE id=%s", (note_id,)
    )
    return {"ok": True}


@router.delete("/knowledge/{note_id}")
async def delete_device_note(note_id: int, session: dict = Depends(require_admin)):
    """Delete a device knowledge entry."""
    await execute("DELETE FROM device_knowledge WHERE id=%s", (note_id,))
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# F5 — EMPLOYEE PERFORMANCE DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/performance/{employee_id}")
async def get_employee_performance(
    employee_id: str,
    session: dict = Depends(get_session),
):
    """
    Return accuracy stats for an employee across all workflow domains.
    Shows task_type, domain, correct/total, and accuracy percentage.
    Only includes rows with at least 1 outcome recorded.
    """
    if employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")

    rows = await fetch_all(
        "SELECT task_type, domain, correct_count, total_count, updated_at "
        "FROM employee_performance "
        "WHERE employee_id=%s AND total_count >= 1 "
        "ORDER BY total_count DESC, updated_at DESC",
        (employee_id,),
    )

    total_correct = 0
    total_runs    = 0
    stats = []
    for r in rows:
        total  = int(r.get("total_count", 0))
        correct = int(r.get("correct_count", 0))
        accuracy = round(correct / total * 100, 1) if total > 0 else 0.0
        total_correct += correct
        total_runs    += total
        stats.append({
            "task_type":     r.get("task_type"),
            "domain":        r.get("domain"),
            "correct_count": correct,
            "total_count":   total,
            "accuracy_pct":  accuracy,
            "updated_at":    str(r.get("updated_at", "")),
        })

    overall_accuracy = round(total_correct / total_runs * 100, 1) if total_runs > 0 else None

    return {
        "employee_id":      employee_id,
        "overall_accuracy": overall_accuracy,
        "total_runs":       total_runs,
        "total_correct":    total_correct,
        "breakdown":        stats,
    }


@router.get("/performance")
async def get_all_performance(session: dict = Depends(get_session)):
    """Return a summary accuracy for all 4 employees."""
    result = {}
    for emp_id in ("aria", "nexus", "cipher", "vega"):
        rows = await fetch_all(
            "SELECT correct_count, total_count FROM employee_performance "
            "WHERE employee_id=%s AND total_count >= 1",
            (emp_id,),
        )
        total   = sum(int(r["total_count"])   for r in rows)
        correct = sum(int(r["correct_count"]) for r in rows)
        result[emp_id] = {
            "total_runs":       total,
            "total_correct":    correct,
            "overall_accuracy": round(correct / total * 100, 1) if total else None,
        }
    return result


# ══════════════════════════════════════════════════════════════════════════════
# EMPLOYEE TYPE CATALOGUE
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/employee-types")
async def list_employee_types(session: dict = Depends(get_session)):
    """Return all available employee type templates."""
    result = []
    for key, meta in EMPLOYEE_TYPES.items():
        instructions = EMPLOYEE_TYPE_INSTRUCTIONS.get(key, {})
        result.append({
            "key":         key,
            "label":       meta["label"],
            "icon":        meta["icon"],
            "desc":        meta["desc"],
            "instructions": {
                "identity":      instructions.get("identity",      ""),
                "expertise":     instructions.get("expertise",     ""),
                "communication": instructions.get("communication", ""),
                "constraints":   instructions.get("constraints",   ""),
            },
        })
    return result


@router.get("/employee-types/{type_key}/instructions")
async def get_type_instructions(type_key: str, session: dict = Depends(get_session)):
    """Get the default instructions for a specific employee type."""
    if type_key not in EMPLOYEE_TYPES:
        raise HTTPException(404, f"Unknown employee type: {type_key}")
    instructions = EMPLOYEE_TYPE_INSTRUCTIONS.get(type_key, {})
    return {
        "key":           type_key,
        "label":         EMPLOYEE_TYPES[type_key]["label"],
        "identity":      instructions.get("identity",      ""),
        "expertise":     instructions.get("expertise",     ""),
        "communication": instructions.get("communication", ""),
        "constraints":   instructions.get("constraints",   ""),
    }


class EmployeeTypeUpdateBody(BaseModel):
    employee_type: str


@router.put("/profiles/{employee_id}/type")
async def update_employee_type(
    employee_id: str,
    body: EmployeeTypeUpdateBody,
    session: dict = Depends(require_admin),
):
    """Update the employee type for an employee and optionally load type defaults."""
    if employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")
    if body.employee_type not in EMPLOYEE_TYPES:
        raise HTTPException(400, f"Unknown type: {body.employee_type}")

    await execute(
        "UPDATE employee_profiles SET employee_type=%s WHERE id=%s",
        (body.employee_type, employee_id),
    )
    return {"ok": True, "employee_type": body.employee_type}


# ══════════════════════════════════════════════════════════════════════════════
# EMPLOYEE ACTIVITY HISTORY
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/history/{employee_id}")
async def get_employee_history(
    employee_id: str,
    event_type:  Optional[str] = None,   # filter: workflow|incident|message|shift|memory
    limit:       int = 50,
    session: dict = Depends(get_session),
):
    """
    Return a unified chronological activity history for an employee.
    Aggregates: workflow runs, incident updates, peer messages, shift handovers, memories.
    """
    if employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")

    events = []

    # Workflow runs
    if not event_type or event_type == "workflow":
        rows = await fetch_all(
            "SELECT wr.id, wr.workflow_id, wr.ai_response, wr.status, wr.outcome, "
            "       wr.outcome_note, wr.created_at, w.name AS workflow_name "
            "FROM workflow_runs wr "
            "LEFT JOIN workflows w ON w.id=wr.workflow_id "
            "WHERE w.employee_id=%s "
            "ORDER BY wr.created_at DESC LIMIT %s",
            (employee_id, limit),
        )
        for r in rows:
            events.append({
                "event_type":   "workflow",
                "event_id":     r["id"],
                "title":        r.get("workflow_name") or f"Workflow #{r['workflow_id']}",
                "summary":      (r.get("ai_response") or "")[:300],
                "status":       r.get("status"),
                "outcome":      r.get("outcome"),
                "outcome_note": r.get("outcome_note"),
                "created_at":   str(r.get("created_at", "")),
            })

    # Incident updates
    if not event_type or event_type == "incident":
        rows = await fetch_all(
            "SELECT iu.id, iu.incident_id, iu.update_text, iu.update_type, iu.created_at, "
            "       i.title AS incident_title, i.severity, i.status AS inc_status "
            "FROM incident_updates iu "
            "JOIN incidents i ON i.id=iu.incident_id "
            "WHERE iu.employee_id=%s "
            "ORDER BY iu.created_at DESC LIMIT %s",
            (employee_id, limit),
        )
        for r in rows:
            events.append({
                "event_type": "incident",
                "event_id":   r["id"],
                "title":      r.get("incident_title") or f"Incident #{r['incident_id']}",
                "summary":    (r.get("update_text") or "")[:300],
                "status":     r.get("update_type"),
                "severity":   r.get("severity"),
                "inc_status": r.get("inc_status"),
                "created_at": str(r.get("created_at", "")),
            })

    # Peer messages (sent or received)
    if not event_type or event_type == "message":
        rows = await fetch_all(
            "SELECT id, from_employee, to_employee, subject, body, reply, status, created_at "
            "FROM employee_messages "
            "WHERE from_employee=%s OR to_employee=%s "
            "ORDER BY created_at DESC LIMIT %s",
            (employee_id, employee_id, limit),
        )
        for r in rows:
            direction = "sent" if r["from_employee"] == employee_id else "received"
            peer = r["to_employee"] if direction == "sent" else r["from_employee"]
            events.append({
                "event_type": "message",
                "event_id":   r["id"],
                "title":      r.get("subject") or f"{direction.title()} message {peer.upper()}",
                "summary":    (r.get("body") or "")[:300],
                "reply":      (r.get("reply") or "")[:200],
                "status":     r.get("status"),
                "direction":  direction,
                "peer":       peer,
                "created_at": str(r.get("created_at", "")),
            })

    # Shift handovers
    if not event_type or event_type == "shift":
        rows = await fetch_all(
            "SELECT id, shift_date, shift_type, briefing, status, created_at "
            "FROM shift_handover WHERE employee_id=%s "
            "ORDER BY created_at DESC LIMIT %s",
            (employee_id, limit),
        )
        for r in rows:
            events.append({
                "event_type": "shift",
                "event_id":   r["id"],
                "title":      f"Shift {r.get('shift_type','').title()} — {r.get('shift_date','')}",
                "summary":    (r.get("briefing") or "")[:300],
                "status":     r.get("status"),
                "created_at": str(r.get("created_at", "")),
            })

    # Memory entries
    if not event_type or event_type == "memory":
        rows = await fetch_all(
            "SELECT id, task_type, task_summary, key_learnings, created_at "
            "FROM employee_memory WHERE employee_id=%s "
            "ORDER BY created_at DESC LIMIT %s",
            (employee_id, limit),
        )
        for r in rows:
            events.append({
                "event_type": "memory",
                "event_id":   r["id"],
                "title":      r.get("task_summary") or f"Memory — {r.get('task_type','')}",
                "summary":    (r.get("key_learnings") or "")[:300],
                "status":     r.get("task_type"),
                "created_at": str(r.get("created_at", "")),
            })

    # Sort all events by created_at descending and trim
    def _dt_key(ev):
        try:
            return ev["created_at"] or ""
        except Exception:
            return ""

    events.sort(key=_dt_key, reverse=True)
    events = events[:limit]

    # Attach feedback counts
    all_ids_by_type: dict[str, list[int]] = {}
    for ev in events:
        all_ids_by_type.setdefault(ev["event_type"], []).append(ev["event_id"])

    feedback_counts: dict[str, dict[int, int]] = {}
    for etype, ids in all_ids_by_type.items():
        if not ids:
            continue
        placeholders = ",".join(["%s"] * len(ids))
        rows = await fetch_all(
            f"SELECT event_id, COUNT(*) AS cnt FROM employee_feedback "
            f"WHERE employee_id=%s AND event_type=%s AND event_id IN ({placeholders}) "
            f"GROUP BY event_id",
            tuple([employee_id, etype] + ids),
        )
        feedback_counts[etype] = {r["event_id"]: r["cnt"] for r in rows}

    for ev in events:
        ev["feedback_count"] = feedback_counts.get(ev["event_type"], {}).get(ev["event_id"], 0)

    return {"employee_id": employee_id, "events": events}


# ══════════════════════════════════════════════════════════════════════════════
# EMPLOYEE FEEDBACK (human comments on history events)
# ══════════════════════════════════════════════════════════════════════════════

class FeedbackBody(BaseModel):
    employee_id: str
    event_type:  str
    event_id:    int
    comment:     str
    rating:      Optional[int] = None   # 1=wrong, 2=ok, 3=good


@router.post("/feedback")
async def add_feedback(body: FeedbackBody, session: dict = Depends(get_session)):
    """
    Add a human comment/correction to a specific activity history event.
    The comment is also saved to employee_memory so the AI learns from it.
    """
    if body.employee_id not in DEFAULT_PERSONAS:
        raise HTTPException(404, "Unknown employee")

    comment = body.comment.strip()
    if not comment:
        raise HTTPException(400, "comment is required")

    valid_types = {"workflow", "incident", "message", "shift", "memory"}
    if body.event_type not in valid_types:
        raise HTTPException(400, f"event_type must be one of: {', '.join(sorted(valid_types))}")

    if body.rating is not None and body.rating not in (1, 2, 3):
        raise HTTPException(400, "rating must be 1 (wrong), 2 (ok), or 3 (good)")

    feedback_id = await execute(
        "INSERT INTO employee_feedback (employee_id, event_type, event_id, comment, rating, created_by) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (body.employee_id, body.event_type, body.event_id, comment, body.rating, "operator"),
    )

    # Save feedback to memory so AI learns from it
    rating_labels = {1: "WRONG — should not do this", 2: "acceptable but could be improved", 3: "correct approach"}
    rating_str = f" [Rated: {rating_labels.get(body.rating, '')}]" if body.rating else ""
    from app.services.memory import save_memory_direct
    import asyncio
    asyncio.create_task(save_memory_direct(
        employee_id=body.employee_id,
        task_type=f"feedback_{body.event_type}",
        task_summary=f"Human feedback on {body.event_type} event #{body.event_id}{rating_str}",
        key_learnings=comment[:1000],
    ))

    return {"ok": True, "feedback_id": feedback_id}


@router.get("/feedback/{employee_id}/{event_type}/{event_id}")
async def get_event_feedback(
    employee_id: str,
    event_type:  str,
    event_id:    int,
    session: dict = Depends(get_session),
):
    """Get all feedback comments for a specific history event."""
    rows = await fetch_all(
        "SELECT id, comment, rating, created_by, created_at "
        "FROM employee_feedback "
        "WHERE employee_id=%s AND event_type=%s AND event_id=%s "
        "ORDER BY created_at ASC",
        (employee_id, event_type, event_id),
    )
    for r in rows:
        r["created_at"] = str(r.get("created_at", ""))
    return {"employee_id": employee_id, "event_type": event_type, "event_id": event_id, "feedback": rows}


@router.delete("/feedback/{feedback_id}")
async def delete_feedback(feedback_id: int, session: dict = Depends(require_admin)):
    """Delete a specific feedback entry."""
    await execute("DELETE FROM employee_feedback WHERE id=%s", (feedback_id,))
    return {"ok": True}


# ── Per-Employee AI Engine ─────────────────────────────────────────────────────

class EmpAiModelBody(BaseModel):
    provider: str = ""
    model:    str = ""


@router.patch("/{employee_id}/ai-model")
async def set_employee_ai_model(
    employee_id: str,
    body: EmpAiModelBody,
    session: dict = Depends(require_operator),
):
    """Set (or clear) the AI provider/model override for a specific employee.
    Pass empty strings to remove the override (employee will use the global default).
    """
    emp_id = employee_id.lower()
    if emp_id not in DEFAULT_PERSONAS:
        raise HTTPException(400, f"Unknown employee: {emp_id}")
    provider_val = body.provider.strip() or None
    model_val    = body.model.strip()    or None
    await execute(
        "UPDATE employee_profiles SET ai_provider=%s, ai_model=%s WHERE id=%s",
        (provider_val, model_val, emp_id),
    )
    return {
        "ok":       True,
        "employee": emp_id,
        "provider": provider_val or "(global default)",
        "model":    model_val    or "(global default)",
    }


@router.get("/{employee_id}/ai-model")
async def get_employee_ai_model(employee_id: str, session: dict = Depends(get_session)):
    """Return the per-employee AI provider/model config (with effective values)."""
    emp_id = employee_id.lower()
    if emp_id not in DEFAULT_PERSONAS:
        raise HTTPException(400, f"Unknown employee: {emp_id}")
    cfg     = await fetch_one("SELECT default_ai_provider, default_ai_model FROM zabbix_config LIMIT 1") or {}
    emp_row = await fetch_one("SELECT ai_provider, ai_model FROM employee_profiles WHERE id=%s", (emp_id,)) or {}
    effective_provider = emp_row.get("ai_provider") or cfg.get("default_ai_provider") or "claude"
    effective_model    = emp_row.get("ai_model")    or cfg.get("default_ai_model")    or MODEL_DEFAULTS.get(effective_provider, "")
    return {
        "employee":          emp_id,
        "provider_override": emp_row.get("ai_provider"),
        "model_override":    emp_row.get("ai_model"),
        "effective_provider": effective_provider,
        "effective_model":   effective_model,
    }


# ─── Voice / TTS ────────────────────────────────────────────

_TTS_VOICES = {
    "aria":   "nova",    # warm female
    "nexus":  "onyx",    # deep male
    "cipher": "shimmer", # cool female
    "vega":   "echo",    # clear male
}

class TtsBody(BaseModel):
    text:     str
    employee: Optional[str] = "aria"
    speed:    float = 1.0


@router.post("/tts")
async def text_to_speech(body: TtsBody, session: dict = Depends(get_session)):
    """Convert text to speech using OpenAI TTS. Returns audio/mpeg."""
    cfg = await fetch_one(
        "SELECT openai_key FROM zabbix_config LIMIT 1"
    ) or {}
    key = cfg.get("openai_key", "").strip()
    if not key:
        raise HTTPException(400, "OpenAI API key not configured — go to Settings → AI Providers")

    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    if len(text) > 4096:
        text = text[:4096]

    voice = _TTS_VOICES.get((body.employee or "aria").lower(), "nova")
    speed = max(0.25, min(4.0, body.speed))

    async with httpx.AsyncClient(verify=settings.outbound_tls_verify, timeout=30) as client:
        resp = await client.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "tts-1", "input": text, "voice": voice, "speed": speed},
        )

    if resp.status_code != 200:
        raise HTTPException(502, f"OpenAI TTS error {resp.status_code}: {resp.text[:200]}")

    return Response(
        content=resp.content,
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )
