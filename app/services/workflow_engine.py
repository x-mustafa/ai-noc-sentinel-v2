"""
Workflow engine: APScheduler-based trigger system.
Handles schedule-based and alarm-based workflow triggers.
"""
import asyncio
import json
import logging
import time
from typing import Any

from app.database import fetch_all, execute
from app.services.zabbix_client import call_zabbix
from app.services.ai_stream import stream_ai

logger = logging.getLogger(__name__)

_scheduler = None
_last_alarm_ids: set[str] = set()


def get_scheduler():
    global _scheduler
    if _scheduler is None:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            _scheduler = AsyncIOScheduler()
        except ImportError:
            logger.warning("APScheduler not installed — workflow scheduling unavailable")
    return _scheduler


async def start_engine():
    """Called on app startup. Load scheduled workflows and start the scheduler."""
    sched = get_scheduler()
    if not sched:
        return

    await _register_scheduled_workflows()
    sched.add_job(_poll_alarm_workflows, "interval", seconds=30, id="alarm_poll", replace_existing=True)
    sched.start()
    logger.info("Workflow engine started.")


async def stop_engine():
    sched = get_scheduler()
    if sched and sched.running:
        sched.shutdown(wait=False)


async def _register_scheduled_workflows():
    sched = get_scheduler()
    if not sched:
        return
    workflows = await fetch_all(
        "SELECT * FROM workflows WHERE is_active=1 AND trigger_type='schedule'"
    )
    for wf in workflows:
        _register_schedule(sched, wf)


def _register_schedule(sched, wf: dict):
    cfg = {}
    try:
        cfg = json.loads(wf.get("trigger_config") or "{}")
    except Exception:
        pass
    cron_expr = cfg.get("cron", "0 8 * * *")  # default: 8 AM daily
    try:
        from apscheduler.triggers.cron import CronTrigger
        parts = cron_expr.split()
        trigger = CronTrigger(
            minute=parts[0] if len(parts) > 0 else "*",
            hour=parts[1]   if len(parts) > 1 else "*",
            day=parts[2]    if len(parts) > 2 else "*",
            month=parts[3]  if len(parts) > 3 else "*",
            day_of_week=parts[4] if len(parts) > 4 else "*",
        )
        job_id = f"wf_{wf['id']}"
        sched.add_job(
            _run_workflow,
            trigger=trigger,
            id=job_id,
            replace_existing=True,
            args=[wf["id"], {"trigger": "schedule", "cron": cron_expr}],
        )
        logger.info(f"Registered scheduled workflow {wf['id']} ({wf['name']}) — cron: {cron_expr}")
    except Exception as e:
        logger.warning(f"Failed to register workflow {wf['id']}: {e}")


async def _poll_alarm_workflows():
    """Check for new Zabbix alarms and trigger matching workflows."""
    global _last_alarm_ids
    workflows = await fetch_all(
        "SELECT * FROM workflows WHERE is_active=1 AND trigger_type='alarm'"
    )
    if not workflows:
        return

    problems_raw = await call_zabbix("problem.get", {
        "output": ["eventid", "objectid", "name", "severity", "clock"],
        "sortfield": "eventid", "sortorder": "DESC", "limit": 50,
    })
    if not isinstance(problems_raw, list):
        return

    new_ids = {p["eventid"] for p in problems_raw if isinstance(p, dict)}
    truly_new = new_ids - _last_alarm_ids
    _last_alarm_ids = new_ids

    if not truly_new:
        return

    new_problems = [p for p in problems_raw if p.get("eventid") in truly_new]

    for wf in workflows:
        cfg = {}
        try:
            cfg = json.loads(wf.get("trigger_config") or "{}")
        except Exception:
            pass
        min_sev = int(cfg.get("severity_min", 3))
        host_filter = (cfg.get("host_filter") or "").lower()

        for p in new_problems:
            sev = int(p.get("severity", 0))
            name = (p.get("name") or "").lower()
            if sev < min_sev:
                continue
            if host_filter and host_filter not in name:
                continue
            asyncio.create_task(_run_workflow(wf["id"], {"trigger": "alarm", "problem": p}))


async def _run_workflow(workflow_id: int, trigger_data: dict):
    """Execute a single workflow: get AI analysis → execute action → log run."""
    wf = await fetch_all("SELECT * FROM workflows WHERE id=%s LIMIT 1", (workflow_id,))
    if not wf:
        return
    wf = wf[0]

    run_id = await execute(
        "INSERT INTO workflow_runs (workflow_id, trigger_data, status) VALUES (%s,%s,'running')",
        (workflow_id, json.dumps(trigger_data, default=str)),
    )

    emp_id = wf.get("employee_id") or "aria"

    # Set employee status to busy while processing the workflow
    try:
        from app.services.employee_context import set_employee_busy, set_employee_available
        await set_employee_busy(emp_id, f"Workflow: {wf.get('name', '')[:80]}")
    except Exception:
        pass

    ai_response = ""
    try:
        ai_response = await _get_ai_response(wf, trigger_data)
        action_result = await _execute_action(wf, trigger_data, ai_response)
        await execute(
            "UPDATE workflow_runs SET ai_response=%s, action_result=%s, status='success' WHERE id=%s",
            (ai_response[:4000] if ai_response else "", action_result, run_id),
        )

        # Auto-save what was found as employee memory (no extra AI call)
        if ai_response.strip():
            asyncio.create_task(
                _auto_save_workflow_memory(emp_id, wf, trigger_data, ai_response)
            )

        # Anomaly detection: did the AI flag something beyond the trigger?
        asyncio.create_task(
            _check_and_announce_anomaly(emp_id, wf, trigger_data, ai_response)
        )

    except Exception as e:
        logger.error(f"Workflow {workflow_id} run failed: {e}")
        await execute(
            "UPDATE workflow_runs SET status='error', action_result=%s WHERE id=%s",
            (str(e)[:500], run_id),
        )
    finally:
        # Always restore employee to available after workflow completes
        try:
            from app.services.employee_context import set_employee_available
            await set_employee_available(emp_id)
        except Exception:
            pass


async def _get_ai_response(wf: dict, trigger_data: dict) -> str:
    from app.database import fetch_one
    from app.services.employee_prompt import build_employee_system_prompt
    from app.services.employee_context import get_full_operational_context

    prompt_tmpl = wf.get("prompt_template") or "Analyze this event and provide a brief assessment."
    prompt = prompt_tmpl
    host_name = None
    if "problem" in trigger_data:
        p = trigger_data["problem"]
        host_name = str(p.get("hosts", ["?"])[0] if p.get("hosts") else "?")
        if host_name == "?":
            host_name = None
        prompt = (prompt_tmpl
                  .replace("{alarm_name}", p.get("name", ""))
                  .replace("{host}", host_name or "unknown")
                  .replace("{severity}", str(p.get("severity", 0))))

    cfg = await fetch_one("SELECT * FROM zabbix_config LIMIT 1") or {}
    provider = cfg.get("default_ai_provider") or "claude"
    model    = cfg.get("default_ai_model")    or ""
    key_map  = {"claude": "claude_key", "openai": "openai_key",
                "gemini": "gemini_key", "grok": "grok_key", "openrouter": "openrouter_key"}
    api_key  = cfg.get(key_map.get(provider, "claude_key"), "")

    if not api_key:
        return "[No AI key configured]"

    model_defaults = {"claude": "claude-haiku-4-5-20251001", "openai": "gpt-4o-mini",
                      "gemini": "gemini-2.0-flash", "grok": "grok-2-latest",
                      "openrouter": "anthropic/claude-haiku-4-5"}
    model = model or model_defaults.get(provider, "claude-haiku-4-5-20251001")

    # Load the assigned employee's full persona, fall back to generic
    emp_id = wf.get("employee_id") or "aria"
    persona = await build_employee_system_prompt(emp_id)

    # Load operational context: open incidents + shift + device knowledge for this host
    ops_ctx = await get_full_operational_context(emp_id, host=host_name)

    # F6 — Inject matching approved runbooks into the prompt
    runbook_ctx = ""
    alarm_text = trigger_data.get("problem", {}).get("name", "") if "problem" in trigger_data else ""
    if alarm_text:
        try:
            from app.routers.runbooks import find_matching_runbooks, format_runbook_for_prompt
            matching = await find_matching_runbooks(alarm_text, limit=2)
            if matching:
                runbook_ctx = "\n\n---- RELEVANT RUNBOOKS ----"
                for rb in matching:
                    runbook_ctx += format_runbook_for_prompt(rb)
                runbook_ctx += "\n---- END RUNBOOKS ----"
        except Exception as e:
            logger.debug(f"Runbook injection failed (non-fatal): {e}")

    system = (
        (persona or "You are NOC Sentinel AI for Tabadul payment infrastructure.")
        + ops_ctx
        + runbook_ctx
        + "\n\nWORKFLOW MODE: You are responding to an automated trigger. "
        "Be concise and actionable. Lead with the most critical finding. "
        "No preamble, no closing remarks."
    )

    full_response = ""
    async for chunk in stream_ai(provider, api_key, model, system, prompt):
        if "data" in chunk:
            try:
                d = json.loads(chunk["data"])
                full_response += d.get("t", "")
            except Exception:
                pass

    return full_response


async def _execute_action(wf: dict, trigger_data: dict, ai_response: str) -> str:
    """Execute one or more actions for a workflow run.

    action_type can be:
      - A JSON array string:  '["teams_chat","incident"]'
      - A legacy single string: "log"
    action_config is a nested dict keyed by action type for multi-action workflows,
    or a flat dict for legacy single-action workflows.
    """
    raw_type = wf.get("action_type") or "log"
    try:
        action_types = json.loads(raw_type)
        if isinstance(action_types, str):
            action_types = [action_types]
    except Exception:
        action_types = [raw_type]

    raw_cfg: dict = {}
    try:
        raw_cfg = json.loads(wf.get("action_config") or "{}")
    except Exception:
        pass

    results = []
    for action_type in action_types:
        # Per-action config: use sub-dict if present, else fall back to root (legacy)
        per_cfg = raw_cfg.get(action_type)
        cfg = per_cfg if isinstance(per_cfg, dict) else raw_cfg
        try:
            result = await _run_single_action(action_type, cfg, wf, trigger_data, ai_response)
        except Exception as e:
            result = f"error: {e}"
        results.append(f"{action_type}: {result}")

    return " | ".join(results)


async def _run_single_action(
    action_type: str, cfg: dict, wf: dict, trigger_data: dict, ai_response: str
) -> str:
    """Execute a single action type and return a short status string."""

    if action_type == "log":
        logger.info(f"[WF {wf['id']}] {wf['name']}: {ai_response[:200]}")
        return "logged"

    elif action_type == "webhook":
        url = cfg.get("url", "")
        if not url:
            return "error: no webhook URL"
        import httpx
        body = {
            "workflow": wf["name"],
            "trigger": trigger_data,
            "ai_response": ai_response,
        }
        try:
            async with httpx.AsyncClient(verify=False, timeout=10) as client:
                resp = await client.post(url, json=body,
                                          headers=cfg.get("headers", {}))
            return f"HTTP {resp.status_code}"
        except Exception as e:
            return f"error: {e}"

    elif action_type == "zabbix_ack":
        p = trigger_data.get("problem", {})
        eventid = p.get("eventid")
        if eventid:
            await call_zabbix("event.acknowledge", {
                "eventids": [eventid],
                "action": 6,
                "message": f"Auto-acknowledged by workflow '{wf['name']}': {ai_response[:200]}",
            })
            return f"acknowledged event {eventid}"
        return "no eventid"

    elif action_type == "whatsapp_group":
        emp_id    = cfg.get("emp_id", "aria")
        group_jid = cfg.get("group_jid", "")
        if not group_jid:
            return "error: no group JID configured"
        import httpx
        wa_service = "http://localhost:3001"
        msg = ai_response[:3800] if ai_response else "[No response]"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{wa_service}/send/{emp_id}",
                    json={"to": group_jid, "message": msg},
                )
            return f"HTTP {resp.status_code}"
        except Exception as e:
            return f"error: {e}"

    elif action_type == "email":
        from app.services.ms365 import send_email
        to_raw  = cfg.get("to", "")
        subject = cfg.get("subject", f"NOC Sentinel — {wf['name']}")
        emp_id  = cfg.get("emp_id", wf.get("employee_id", "aria"))
        cc_raw  = cfg.get("cc", "")
        if not to_raw:
            return "error: no recipient email"
        to_list = [t.strip() for t in to_raw.split(",") if t.strip()]
        cc_list = [c.strip() for c in cc_raw.split(",") if c.strip()] if cc_raw else None
        result = await send_email(to=to_list, subject=subject, body=ai_response,
                                  employee_id=emp_id, cc=cc_list)
        return "sent" if result["ok"] else result.get("error", "failed")

    elif action_type == "teams":
        from app.services.ms365 import send_teams_message
        webhook_url = cfg.get("webhook_url", "")
        title       = cfg.get("title", wf["name"])
        emp_id      = cfg.get("emp_id", wf.get("employee_id", "aria"))
        if not webhook_url:
            return "error: no Teams webhook URL"
        result = await send_teams_message(webhook_url=webhook_url, message=ai_response,
                                          title=title, employee_id=emp_id)
        return "sent" if result["ok"] else result.get("error", "failed")

    elif action_type == "teams_chat":
        from app.services.ms365 import send_to_chat
        chat_id = cfg.get("chat_id", "")
        emp_id  = cfg.get("emp_id", wf.get("employee_id", "aria"))
        if not chat_id:
            return "error: no Teams chat ID"
        result = await send_to_chat(chat_id=chat_id, message=ai_response, employee_id=emp_id)
        return "sent" if result["ok"] else result.get("error", "failed")

    elif action_type == "teams_channel":
        from app.services.ms365 import send_to_channel
        team_id    = cfg.get("team_id", "")
        channel_id = cfg.get("channel_id", "")
        emp_id     = cfg.get("emp_id", wf.get("employee_id", "aria"))
        title      = cfg.get("title", wf["name"])
        if not team_id or not channel_id:
            return "error: no Teams team/channel ID"
        result = await send_to_channel(team_id=team_id, channel_id=channel_id,
                                       message=ai_response, title=title, employee_id=emp_id)
        return "sent" if result["ok"] else result.get("error", "failed")

    elif action_type == "whatsapp_dm":
        emp_id = cfg.get("emp_id", "aria")
        to_jid = cfg.get("to_jid", "")
        if not to_jid:
            return "error: no WhatsApp JID"
        import httpx
        wa_service = "http://localhost:3001"
        msg = ai_response[:3800] if ai_response else "[No response]"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{wa_service}/send/{emp_id}",
                    json={"to": to_jid, "message": msg},
                )
            return f"HTTP {resp.status_code}"
        except Exception as e:
            return f"error: {e}"

    elif action_type == "incident":
        severity  = int(cfg.get("severity", 3))
        raw_title = cfg.get("title") or f"Workflow: {wf['name']}"
        title     = raw_title.replace("{workflow_name}", wf["name"])
        owner_id  = cfg.get("owner_id") or wf.get("employee_id") or "aria"
        prob      = trigger_data.get("problem", {})
        hosts     = prob.get("hosts") or []
        host      = hosts[0] if hosts else None
        zabbix_id = str(prob.get("eventid") or "") or None
        inc_id = await execute(
            "INSERT INTO incidents "
            "(title, description, owner_id, severity, host, zabbix_event_id, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,'workflow')",
            (title, ai_response[:2000], owner_id, severity, host, zabbix_id),
        )
        return f"created INC-{inc_id:04d}"

    return f"unknown action type"


async def trigger_workflow_manually(workflow_id: int) -> dict:
    """Manually trigger a workflow. Returns run status."""
    await _run_workflow(workflow_id, {"trigger": "manual", "ts": int(time.time())})
    return {"ok": True, "workflow_id": workflow_id}


async def reload_scheduled_workflows():
    """Re-register all scheduled workflows (call after create/update/delete)."""
    sched = get_scheduler()
    if not sched:
        return
    # Remove existing workflow jobs
    for job in sched.get_jobs():
        if job.id.startswith("wf_"):
            job.remove()
    await _register_scheduled_workflows()


# ── Auto-memory: save workflow findings without a second AI call ───────────────

async def _auto_save_workflow_memory(
    emp_id: str,
    wf: dict,
    trigger_data: dict,
    ai_response: str,
) -> None:
    """Store the workflow outcome in the employee's long-term memory."""
    try:
        from app.services.memory import save_memory_direct

        trigger_type = wf.get("trigger_type", "manual")
        wf_name      = wf.get("name", "Workflow")

        # Build task summary from trigger context
        if "problem" in trigger_data:
            p = trigger_data["problem"]
            summary = f"{wf_name}: alarm '{p.get('name','?')}' sev {p.get('severity','?')}"
        else:
            summary = f"{wf_name} ({trigger_type} trigger)"

        # Extract key learnings: first 3 distinct sentences from the AI response
        sentences = [s.strip() for s in ai_response.replace("\n", " ").split(".") if len(s.strip()) > 20]
        learnings = ". ".join(sentences[:3])
        if not learnings:
            learnings = ai_response[:300]

        await save_memory_direct(emp_id, f"workflow_{trigger_type}", summary, learnings)
    except Exception as e:
        logger.debug(f"_auto_save_workflow_memory failed (non-fatal): {e}")


# ── Anomaly detector: auto-announce findings beyond the trigger ────────────────

# Keywords that signal an anomaly was detected in the AI response
_ANOMALY_SIGNALS = {
    "critical", "breached", "anomaly", "anomalous", "unusual", "unexpected",
    "suspicious", "investigate immediately", "immediately investigate",
    "out of pattern", "not seen before", "first time", "escalate",
    "high risk", "at risk", "sla breach", "sla at risk",
    "multiple hosts", "spread", "cascade", "correlated",
    "unauthorized", "attack", "intrusion", "threat detected",
    "data exfiltration", "lateral movement", "compromise",
    "packet loss", "link down", "failover", "fail-over", "ha split",
    "gateway unreachable", "payment gateway", "transaction failure",
    "bgp flap", "route withdrawn", "isp down",
}

# Which employee to auto-notify for different anomaly types
_ANOMALY_ROUTING = {
    "security": "cipher",
    "network":  "nexus",
    "sla":      "vega",
    "default":  "aria",
}


def _detect_anomaly(ai_response: str) -> tuple[bool, str]:
    """
    Scan AI response for anomaly signals.
    Returns (is_anomaly, severity_label).
    """
    lower = ai_response.lower()
    hits  = [kw for kw in _ANOMALY_SIGNALS if kw in lower]
    if not hits:
        return False, ""

    # Classify severity by keyword type
    critical_kws = {"breached", "sla breach", "attack", "intrusion", "compromise",
                    "data exfiltration", "lateral movement", "payment gateway",
                    "transaction failure", "ha split"}
    if any(kw in hits for kw in critical_kws):
        return True, "CRITICAL"
    return True, "HIGH"


async def _check_and_announce_anomaly(
    emp_id: str,
    wf: dict,
    trigger_data: dict,
    ai_response: str,
) -> None:
    """
    After a workflow run, detect if the AI flagged something anomalous
    beyond the original trigger. If yes, auto-create a peer message to
    the most relevant colleague and log it.
    """
    try:
        is_anomaly, severity = _detect_anomaly(ai_response)
        if not is_anomaly:
            return

        # Determine the best colleague to notify (not same as current employee)
        lower = ai_response.lower()
        if any(kw in lower for kw in {"attack", "unauthorized", "intrusion", "threat", "compromise",
                                       "exfiltration", "lateral", "firewall", "ids", "ips"}):
            notify_emp = "cipher"
        elif any(kw in lower for kw in {"bgp", "isp", "link down", "failover", "ha split",
                                          "route", "interface", "latency", "packet loss"}):
            notify_emp = "nexus"
        elif any(kw in lower for kw in {"sla", "breach", "error budget", "slo", "uptime"}):
            notify_emp = "vega"
        else:
            notify_emp = "aria"

        # Don't notify yourself
        if notify_emp == emp_id:
            candidates = [e for e in ("aria", "nexus", "cipher", "vega") if e != emp_id]
            notify_emp = candidates[0] if candidates else "aria"

        wf_name = wf.get("name", "Workflow")
        alarm_name = ""
        if "problem" in trigger_data:
            alarm_name = trigger_data["problem"].get("name", "")

        subject = f"[{severity}] Anomaly detected during '{wf_name}'"
        body    = (
            f"While running workflow '{wf_name}', I detected something beyond the "
            f"original trigger that requires attention.\n\n"
            f"Original alarm: {alarm_name or '(scheduled/manual)'}\n\n"
            f"Finding:\n{ai_response[:1200]}\n\n"
            f"Please review and take action if needed."
        )

        # Save as peer message (will auto-generate a reply from the notified employee)
        from app.database import execute as db_execute
        await db_execute(
            "INSERT INTO employee_messages "
            "(from_employee, to_employee, subject, body, initiated_by) "
            "VALUES (%s,%s,%s,%s,'auto-anomaly')",
            (emp_id, notify_emp, subject[:300], body[:3000]),
        )
        logger.warning(
            f"[ANOMALY] {emp_id.upper()} → {notify_emp.upper()}: "
            f"{severity} anomaly during '{wf_name}' — auto-message sent"
        )

    except Exception as e:
        logger.debug(f"_check_and_announce_anomaly failed (non-fatal): {e}")
