"""
Workflow engine: APScheduler-based trigger system.
Handles schedule-based and alarm-based workflow triggers.
"""
import asyncio
import json
import logging
import time
from typing import Any

from app.config import settings
from app.database import fetch_all, fetch_one, execute
from app.services.ai_provider import provider_candidates, resolve_runtime_ai
from app.services.ai_stream import extract_error_chunk, extract_text_chunk
from app.services.observability import collect_monitoring_snapshot, snapshot_prompt_context
from app.services.zabbix_client import call_zabbix
from app.services.ai_stream import stream_ai

logger = logging.getLogger(__name__)

_scheduler = None
_last_alarm_ids: set[str] = set()

_RISK_ORDER = {
    "observe": 0,
    "safe_auto": 1,
    "approval_required": 2,
    "forbidden": 3,
}

_ACTION_MIN_RISK = {
    "log": "observe",
    "webhook": "safe_auto",
    "email": "safe_auto",
    "teams": "safe_auto",
    "teams_chat": "safe_auto",
    "teams_channel": "safe_auto",
    "whatsapp_group": "safe_auto",
    "whatsapp_dm": "safe_auto",
    "incident": "safe_auto",
    "escalation": "safe_auto",
    "zabbix_ack": "approval_required",
}


def get_scheduler():
    global _scheduler
    if _scheduler is None:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            _scheduler = AsyncIOScheduler()
        except ImportError:
            logger.warning("APScheduler not installed — workflow scheduling unavailable")
    return _scheduler


def _normalize_risk_tier(value: str | None) -> str:
    if value in _RISK_ORDER:
        return value
    return "safe_auto"


def _higher_risk(first: str, second: str) -> str:
    if _RISK_ORDER[first] >= _RISK_ORDER[second]:
        return first
    return second


def _parse_action_types(wf: dict) -> list[str]:
    raw_type = wf.get("action_type") or "log"
    try:
        action_types = json.loads(raw_type)
        if isinstance(action_types, str):
            action_types = [action_types]
    except Exception:
        action_types = [raw_type]
    return [str(item).strip() for item in action_types if str(item).strip()]


def _parse_action_config(wf: dict) -> dict:
    try:
        parsed = json.loads(wf.get("action_config") or "{}")
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {}


def _required_risk_for_actions(action_types: list[str]) -> str:
    required = "observe"
    for action_type in action_types:
        required = _higher_risk(required, _ACTION_MIN_RISK.get(action_type, "forbidden"))
    return required


def _effective_risk_tier(wf: dict, action_types: list[str]) -> str:
    configured = _normalize_risk_tier(wf.get("risk_tier"))
    if configured == "forbidden":
        return "forbidden"
    return _higher_risk(configured, _required_risk_for_actions(action_types))


def _describe_action_plan(action_types: list[str], raw_cfg: dict, wf: dict) -> str:
    plan = []
    for action_type in action_types:
        per_cfg = raw_cfg.get(action_type)
        cfg = per_cfg if isinstance(per_cfg, dict) else raw_cfg
        target = ""
        for key in ("url", "to", "webhook_url", "chat_id", "team_id", "channel_id", "group_jid", "to_jid", "owner_id", "escalated_to"):
            if cfg.get(key):
                target = str(cfg[key])
                break
        if not target and action_type == "incident":
            target = wf.get("employee_id") or "aria"
        plan.append(f"{action_type} -> {target}" if target else action_type)
    return "; ".join(plan) or "log"


def _response_signals_normal(ai_response: str) -> bool:
    text = (ai_response or "").strip().upper()
    return text.startswith("STATUS: NORMAL") or "NO_ABNORMALITIES" in text


async def start_engine():
    """Called on app startup. Load scheduled workflows and start the scheduler."""
    sched = get_scheduler()
    if not sched:
        return False
    if sched.running:
        logger.info("Workflow engine already running.")
        return True

    await _register_scheduled_workflows()
    sched.add_job(_refresh_scheduled_workflows_job, "interval", minutes=1, id="schedule_refresh", replace_existing=True)
    sched.add_job(_poll_alarm_workflows, "interval", seconds=30,  id="alarm_poll",       replace_existing=True)
    sched.add_job(_watchlist_scan_job,   "interval", hours=4,     id="watchlist_scan",   replace_existing=True)
    sched.add_job(_escalation_followup,  "interval", minutes=5,   id="esc_followup",     replace_existing=True)
    sched.add_job(_change_auto_activate, "interval", minutes=5,   id="change_activate",  replace_existing=True)
    sched.add_job(_self_improvement_job, "cron",     day_of_week="mon", hour=6,           id="self_improve",    replace_existing=True)
    sched.start()
    logger.info("Workflow engine started (alarm poll, watchlist scan, escalation follow-up, change calendar, self-improvement).")
    return True


async def _refresh_scheduled_workflows_job():
    try:
        await reload_scheduled_workflows()
    except Exception as e:
        logger.warning(f"Workflow schedule refresh failed: {e}")


async def _watchlist_scan_job():
    try:
        from app.routers.watchlist import run_watchlist_scan_all
        await run_watchlist_scan_all()
    except Exception as e:
        logger.error(f"Watchlist scan job error: {e}")


async def _escalation_followup():
    try:
        from app.routers.escalations import run_escalation_followup
        await run_escalation_followup()
    except Exception as e:
        logger.error(f"Escalation follow-up job error: {e}")


async def _change_auto_activate():
    try:
        from app.routers.changes import auto_activate_changes
        await auto_activate_changes()
    except Exception as e:
        logger.error(f"Change auto-activate job error: {e}")


async def _self_improvement_job():
    """F9 — Weekly self-improvement review for each employee (runs Monday 06:00)."""
    try:
        await _run_self_improvement_all()
    except Exception as e:
        logger.error(f"Self-improvement job error: {e}")


async def _run_self_improvement_all():
    """Ask each employee to review their last week and suggest improvements."""
    from app.database import fetch_one, fetch_all as db_fetch_all, execute as db_exec
    from app.services.ai_stream import stream_ai
    from app.services.employee_prompt import build_employee_system_prompt

    cfg = await fetch_one("SELECT * FROM zabbix_config LIMIT 1")
    if not cfg:
        return

    for emp_id in ("aria", "nexus", "cipher", "vega"):
        try:
            # Gather last 7 days of data
            runs = await db_fetch_all(
                "SELECT w.name, wr.status, wr.outcome, wr.created_at "
                "FROM workflow_runs wr JOIN workflows w ON wr.workflow_id=w.id "
                "WHERE w.employee_id=%s AND wr.created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY) "
                "ORDER BY wr.created_at DESC LIMIT 30",
                (emp_id,),
            )
            incidents = await db_fetch_all(
                "SELECT title, status, severity, started_at FROM incidents "
                "WHERE owner_id=%s AND started_at >= DATE_SUB(NOW(), INTERVAL 7 DAY) LIMIT 20",
                (emp_id,),
            )
            perf = await db_fetch_all(
                "SELECT task_type, domain, correct_count, total_count FROM employee_performance "
                "WHERE employee_id=%s ORDER BY total_count DESC LIMIT 10",
                (emp_id,),
            )

            # Summarise data for prompt
            runs_summary = "\n".join([
                f"  - {r['name']}: {r['status']} | outcome: {r.get('outcome','unknown')}"
                for r in runs
            ]) or "  (no workflow runs this week)"

            incidents_summary = "\n".join([
                f"  - [{r['severity']}] {r['title']} → {r['status']}"
                for r in incidents
            ]) or "  (no incidents this week)"

            perf_summary = "\n".join([
                f"  - {p['task_type']}/{p['domain']}: "
                f"{p['correct_count']}/{p['total_count']} correct"
                for p in perf
            ]) or "  (no performance data)"

            prompt = (
                f"You are {emp_id.upper()}. Review your last 7 days of work and suggest improvements.\n\n"
                f"WORKFLOW RUNS (last 7 days):\n{runs_summary}\n\n"
                f"INCIDENTS YOU OWNED:\n{incidents_summary}\n\n"
                f"YOUR ACCURACY STATS:\n{perf_summary}\n\n"
                f"Based on this data, suggest exactly 5 specific improvements:\n"
                f"1. A new workflow to automate repeated work\n"
                f"2. A runbook to write for an unhandled scenario\n"
                f"3. An instruction update that would make you more accurate\n"
                f"4. A Zabbix monitoring gap you noticed\n"
                f"5. One other improvement specific to your role\n\n"
                f"Be specific. Reference real workflow names, hosts, or patterns you see in the data. "
                f"No preamble. Start with '1.'"
            )

            sys_prompt = await build_employee_system_prompt(emp_id)
            provider, model, api_key = resolve_runtime_ai(
                cfg,
                (cfg or {}).get("default_ai_provider"),
                (cfg or {}).get("default_ai_model"),
            )
            if not api_key:
                continue
            chunks = []
            async for chunk in stream_ai(
                provider, api_key, model,
                sys_prompt, prompt,
            ):
                text = extract_text_chunk(chunk)
                if text:
                    chunks.append(text)
            report = "".join(chunks).strip()

            # Store as a high-weight memory entry
            from app.services.memory import save_memory_direct
            await save_memory_direct(
                emp_id,
                task_type="self_improvement",
                task_summary=f"Weekly self-review — {len(runs)} workflow runs, {len(incidents)} incidents",
                key_learnings=report[:1000],
                source="self_review",
                weight=2,
            )

            # Update last_self_review timestamp
            await db_exec(
                "UPDATE employee_profiles SET last_self_review=NOW() WHERE id=%s",
                (emp_id,),
            )

            logger.info(f"[SELF-IMPROVE] {emp_id.upper()} weekly review complete.")

        except Exception as e:
            logger.error(f"[SELF-IMPROVE] {emp_id} error: {e}")


async def stop_engine():
    global _scheduler
    sched = _scheduler
    if sched and sched.running:
        sched.shutdown(wait=False)
    _scheduler = None


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

    # ── Alert Rules Engine: evaluate each new problem against custom rules ──
    if new_problems:
        try:
            from app.routers.alert_rules import evaluate_rules_for_alarm
            for p in new_problems:
                asyncio.create_task(evaluate_rules_for_alarm(p))
        except Exception as e:
            logger.warning(f"Alert rules evaluation error: {e}")


async def _run_workflow_for_alarm(employee_id: str, alarm: dict):
    """
    Trigger an ad-hoc AI analysis of an alarm by a specific employee.
    Used by the alert rules engine (assign_employee action).
    """
    try:
        from app.database import fetch_one as _fetch
        from app.services.ai_stream import stream_ai
        from app.services.employee_prompt import build_employee_system_prompt

        cfg = await _fetch("SELECT * FROM zabbix_config LIMIT 1")
        if not cfg:
            return

        emp_row = await _fetch("SELECT ai_provider, ai_model FROM employee_profiles WHERE id=%s", (employee_id,))
        provider, model, api_key = resolve_runtime_ai(
            cfg,
            (emp_row or {}).get("ai_provider") or cfg.get("default_ai_provider"),
            (emp_row or {}).get("ai_model") or cfg.get("default_ai_model"),
        )
        if not api_key:
            return

        sys_prompt = await build_employee_system_prompt(employee_id)
        alarm_name = alarm.get("name", "Unknown alarm")
        severity   = alarm.get("severity", "?")
        prompt = (
            f"ALERT RULE triggered. Analyze this alarm immediately:\n"
            f"Alarm: {alarm_name}\nSeverity: {severity}\n"
            f"Event ID: {alarm.get('eventid','?')}\n\n"
            "Provide: severity assessment, probable cause, and immediate action recommendation."
        )

        chunks = []
        async for chunk in stream_ai(provider, api_key, model, sys_prompt, prompt):
            text = extract_text_chunk(chunk)
            if text:
                chunks.append(text)

        response = "".join(chunks).strip()
        if response:
            from app.services.memory import save_memory_direct
            await save_memory_direct(
                employee_id, "alert_rule_trigger",
                f"Alert rule fired: {alarm_name}",
                response[:500],
                alarm_type=alarm_name,
            )
    except Exception as e:
        logger.warning(f"_run_workflow_for_alarm failed: {e}")


async def _queue_workflow_approval(
    wf: dict,
    run_id: int,
    trigger_data: dict,
    ai_response: str,
    effective_risk_tier: str,
    requested_by: str,
) -> int:
    action_types = _parse_action_types(wf)
    raw_cfg = _parse_action_config(wf)
    action_plan = _describe_action_plan(action_types, raw_cfg, wf)
    existing_pending = await fetch_one(
        "SELECT id FROM workflow_approvals WHERE workflow_id=%s AND status='pending' AND action_plan=%s "
        "AND requested_at >= DATE_SUB(NOW(), INTERVAL 60 MINUTE) "
        "ORDER BY requested_at DESC LIMIT 1",
        (wf["id"], action_plan),
    )
    if existing_pending:
        approval_id = int(existing_pending["id"])
        await execute(
            "UPDATE workflow_runs SET status='awaiting_approval', approval_id=%s, effective_risk_tier=%s, "
            "ai_response=%s, action_result=%s WHERE id=%s",
            (
                approval_id,
                effective_risk_tier,
                ai_response[:4000] if ai_response else "",
                f"Approval already pending: {approval_id} ({action_plan})",
                run_id,
            ),
        )
        return approval_id
    approval_id = await execute(
        "INSERT INTO workflow_approvals "
        "(workflow_id, workflow_run_id, effective_risk_tier, requested_by, trigger_data, ai_response, action_plan, status) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,'pending')",
        (
            wf["id"],
            run_id,
            effective_risk_tier,
            requested_by or "system",
            json.dumps(trigger_data, default=str),
            ai_response[:8000] if ai_response else "",
            action_plan,
        ),
    )
    await execute(
        "UPDATE workflow_runs SET status='awaiting_approval', approval_id=%s, effective_risk_tier=%s, "
        "ai_response=%s, action_result=%s WHERE id=%s",
        (
            approval_id,
            effective_risk_tier,
            ai_response[:4000] if ai_response else "",
            f"Approval required before executing actions: {action_plan}",
            run_id,
        ),
    )
    return approval_id


async def approve_pending_workflow_action(approval_id: int, decided_by: str, decision_note: str | None = None) -> dict:
    approval = await fetch_one("SELECT * FROM workflow_approvals WHERE id=%s", (approval_id,))
    if not approval:
        raise ValueError("Approval request not found")
    if approval.get("status") != "pending":
        raise ValueError("Approval request is no longer pending")
    requested_by = str(approval.get("requested_by") or "").strip().lower()
    decided_by_normalized = str(decided_by or "").strip().lower()
    if requested_by and requested_by not in {"system", "scheduler"} and requested_by == decided_by_normalized:
        raise ValueError("A different operator must approve this workflow action")

    wf = await fetch_one("SELECT * FROM workflows WHERE id=%s", (approval["workflow_id"],))
    if not wf:
        raise ValueError("Workflow no longer exists")
    if _effective_risk_tier(wf, _parse_action_types(wf)) == "forbidden":
        raise ValueError("Workflow is now marked forbidden and cannot be executed")

    trigger_data = {}
    try:
        trigger_data = json.loads(approval.get("trigger_data") or "{}")
    except Exception:
        pass

    ai_response = approval.get("ai_response") or ""
    await execute(
        "UPDATE workflow_approvals SET status='approved', decided_by=%s, decision_note=%s, decided_at=NOW() WHERE id=%s",
        (decided_by, decision_note, approval_id),
    )

    action_result = await _execute_action(wf, trigger_data, ai_response)
    run_status = "error" if "error:" in action_result.lower() else "success"
    await execute(
        "UPDATE workflow_runs SET status=%s, effective_risk_tier=%s, ai_response=%s, action_result=%s WHERE id=%s",
        (
            run_status,
            approval.get("effective_risk_tier") or "approval_required",
            ai_response[:4000] if ai_response else "",
            action_result,
            approval["workflow_run_id"],
        ),
    )
    await execute(
        "UPDATE workflow_approvals SET status='executed', executed_at=NOW() WHERE id=%s",
        (approval_id,),
    )
    return {
        "ok": True,
        "approval_id": approval_id,
        "workflow_id": approval["workflow_id"],
        "run_id": approval["workflow_run_id"],
        "status": "executed",
        "run_status": run_status,
        "action_result": action_result,
    }


async def reject_pending_workflow_action(approval_id: int, decided_by: str, decision_note: str | None = None) -> dict:
    approval = await fetch_one("SELECT * FROM workflow_approvals WHERE id=%s", (approval_id,))
    if not approval:
        raise ValueError("Approval request not found")
    if approval.get("status") != "pending":
        raise ValueError("Approval request is no longer pending")

    note = decision_note or "Rejected by operator"
    await execute(
        "UPDATE workflow_approvals SET status='rejected', decided_by=%s, decision_note=%s, decided_at=NOW() WHERE id=%s",
        (decided_by, note, approval_id),
    )
    await execute(
        "UPDATE workflow_runs SET status='blocked', effective_risk_tier=%s, action_result=%s WHERE id=%s",
        (
            approval.get("effective_risk_tier") or "approval_required",
            f"Rejected by {decided_by}: {note}",
            approval["workflow_run_id"],
        ),
    )
    return {
        "ok": True,
        "approval_id": approval_id,
        "workflow_id": approval["workflow_id"],
        "run_id": approval["workflow_run_id"],
        "status": "rejected",
    }


async def _run_workflow(workflow_id: int, trigger_data: dict, requested_by: str = "system"):
    """Execute a single workflow: get AI analysis → execute action → log run."""
    wf = await fetch_all("SELECT * FROM workflows WHERE id=%s LIMIT 1", (workflow_id,))
    if not wf:
        return {"ok": False, "workflow_id": workflow_id, "error": "Workflow not found"}
    wf = wf[0]
    action_types = _parse_action_types(wf)
    effective_risk_tier = _effective_risk_tier(wf, action_types)

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
        approval_id = None
        result_status = "success"

        if effective_risk_tier == "forbidden":
            action_result = "blocked: forbidden risk tier; no action executed"
            result_status = "blocked"
            await execute(
                "UPDATE workflow_runs SET ai_response=%s, action_result=%s, status=%s, effective_risk_tier=%s WHERE id=%s",
                (
                    ai_response[:4000] if ai_response else "",
                    action_result,
                    result_status,
                    effective_risk_tier,
                    run_id,
                ),
            )
        elif effective_risk_tier == "approval_required":
            approval_id = await _queue_workflow_approval(
                wf,
                run_id,
                trigger_data,
                ai_response,
                effective_risk_tier,
                requested_by or "system",
            )
            action_result = f"approval queued: {approval_id}"
            result_status = "awaiting_approval"
        else:
            action_result = await _execute_action(wf, trigger_data, ai_response)
            result_status = "error" if "error:" in action_result.lower() else "success"
            await execute(
                "UPDATE workflow_runs SET ai_response=%s, action_result=%s, status=%s, effective_risk_tier=%s WHERE id=%s",
                (
                    ai_response[:4000] if ai_response else "",
                    action_result,
                    result_status,
                    effective_risk_tier,
                    run_id,
                ),
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
        return {
            "ok": True,
            "workflow_id": workflow_id,
            "run_id": run_id,
            "status": result_status,
            "effective_risk_tier": effective_risk_tier,
            "approval_id": approval_id,
            "action_result": action_result,
        }

    except Exception as e:
        logger.error(f"Workflow {workflow_id} run failed: {e}")
        await execute(
            "UPDATE workflow_runs SET status='error', effective_risk_tier=%s, action_result=%s WHERE id=%s",
            (effective_risk_tier, str(e)[:500], run_id),
        )
        return {
            "ok": False,
            "workflow_id": workflow_id,
            "run_id": run_id,
            "status": "error",
            "effective_risk_tier": effective_risk_tier,
            "error": str(e)[:500],
        }
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
    candidates = provider_candidates(
        cfg,
        cfg.get("default_ai_provider"),
        cfg.get("default_ai_model"),
        fallback_provider=cfg.get("default_ai_provider") or "claude",
    )

    if not candidates:
        return "[No AI key configured]"

    # Load the assigned employee's full persona, fall back to generic
    emp_id = wf.get("employee_id") or "aria"
    persona = await build_employee_system_prompt(emp_id)

    # Load operational context: open incidents + shift + device knowledge for this host
    ops_ctx = await get_full_operational_context(emp_id, host=host_name)
    observability_ctx = ""
    snapshot = None
    if wf.get("trigger_type") in ("manual", "schedule"):
        try:
            snapshot = await collect_monitoring_snapshot(cfg)
            observability_ctx = "\n\n" + snapshot_prompt_context(snapshot)
        except Exception as e:
            logger.debug(f"Observability snapshot failed (non-fatal): {e}")

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
        + observability_ctx
        + runbook_ctx
        + "\n\nWORKFLOW MODE: You are responding to an automated trigger. "
        "Be concise and actionable. Lead with the most critical finding. "
        "No preamble, no closing remarks."
    )

    attempt_errors: list[str] = []
    for provider, model, api_key in candidates:
        full_response = ""
        first_error = ""
        async for chunk in stream_ai(provider, api_key, model, system, prompt):
            text = extract_text_chunk(chunk)
            if text:
                full_response += text
                continue
            if not first_error:
                first_error = extract_error_chunk(chunk)

        if full_response.strip():
            return full_response

        if first_error:
            attempt_errors.append(f"{provider}: {first_error}")
            logger.warning("Workflow AI provider %s failed, trying fallback.", provider)
        else:
            attempt_errors.append(f"{provider}: returned no text")

    if wf.get("trigger_type") in ("schedule", "alarm"):
        fallback_response = _rules_based_workflow_fallback(trigger_data, snapshot, attempt_errors)
        if fallback_response:
            logger.warning("Workflow %s used rules-based fallback because no AI provider succeeded.", wf.get("id"))
            return fallback_response

    raise RuntimeError("All AI providers failed: " + " | ".join(attempt_errors)[:600])


def _rules_based_workflow_fallback(trigger_data: dict, snapshot: dict | None, attempt_errors: list[str]) -> str:
    failure_note = "AI providers unavailable; using a deterministic monitoring fallback."

    problem = trigger_data.get("problem") if isinstance(trigger_data, dict) else None
    if isinstance(problem, dict) and problem:
        alarm_name = str(problem.get("name") or "Unknown alarm")
        severity = str(problem.get("severity") or "?")
        event_id = str(problem.get("eventid") or "?")
        return (
            "STATUS: ABNORMAL\n"
            f"FINDINGS: Alarm '{alarm_name}' (severity {severity}, event {event_id}) triggered while {failure_note.lower()}\n"
            "IMPACT: Active alarm conditions require operator validation and containment.\n"
            "NEXT ACTION: Review the alarm in Zabbix, confirm customer impact, and follow the matching runbook.\n"
            "ESCALATION: Duty Manager if the issue is user-facing, widespread, or sustained."
        )

    zabbix = (snapshot or {}).get("zabbix") or {}
    problem_count = int(zabbix.get("problem_count") or 0)
    zabbix_status = str(zabbix.get("status") or "unknown")
    top_problems = zabbix.get("top_problems") or []

    if zabbix_status == "error":
        provider_note = ""
        if attempt_errors:
            provider_note = " Provider failures: " + "; ".join(attempt_errors[:2])[:260]
        return (
            "STATUS: ABNORMAL\n"
            f"FINDINGS: Zabbix monitoring could not be read. {failure_note}{provider_note}\n"
            "IMPACT: Current network health cannot be confirmed from automation.\n"
            "NEXT ACTION: Check Zabbix connectivity, API auth, and core monitoring reachability immediately.\n"
            "ESCALATION: NOC platform owner if monitoring remains unavailable."
        )

    if problem_count > 0:
        highlights = ", ".join(
            f"{item.get('name', 'Unknown issue')} (sev {int(item.get('severity') or 0)})"
            for item in top_problems[:3]
        ) or "Multiple active Zabbix problems"
        return (
            "STATUS: ABNORMAL\n"
            f"FINDINGS: Zabbix reports {problem_count} active problem(s). Top signals: {highlights}. {failure_note}\n"
            "IMPACT: One or more monitored services are degraded or at risk until triaged.\n"
            "NEXT ACTION: Validate the top active alarms in Zabbix, confirm whether the same issue is already owned, and update the incident timeline.\n"
            "ESCALATION: Duty Manager if severity is high/disaster, multiple systems are affected, or customer impact is confirmed."
        )

    return (
        "STATUS: NORMAL\n"
        "Zabbix currently reports no active problems. "
        f"{failure_note}"
    )


async def _execute_action(wf: dict, trigger_data: dict, ai_response: str) -> str:
    """Execute one or more actions for a workflow run.

    action_type can be:
      - A JSON array string:  '["teams_chat","incident"]'
      - A legacy single string: "log"
    action_config is a nested dict keyed by action type for multi-action workflows,
    or a flat dict for legacy single-action workflows.
    """
    action_types = _parse_action_types(wf)
    raw_cfg = _parse_action_config(wf)
    normal_signal = _response_signals_normal(ai_response)

    results = []
    runtime_ctx: dict[str, Any] = {}
    for action_type in action_types:
        # Per-action config: use sub-dict if present, else fall back to root (legacy)
        per_cfg = raw_cfg.get(action_type)
        cfg = per_cfg if isinstance(per_cfg, dict) else raw_cfg
        if normal_signal and action_type != "log":
            results.append(f"{action_type}: skipped (STATUS: NORMAL)")
            continue
        try:
            result = await _run_single_action(action_type, cfg, wf, trigger_data, ai_response, runtime_ctx)
        except Exception as e:
            result = f"error: {e}"
        results.append(f"{action_type}: {result}")

    return " | ".join(results)


async def _run_single_action(
    action_type: str, cfg: dict, wf: dict, trigger_data: dict, ai_response: str, runtime_ctx: dict[str, Any]
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
            async with httpx.AsyncClient(verify=settings.outbound_tls_verify, timeout=10) as client:
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
        existing_incident = None
        if zabbix_id:
            existing_incident = await fetch_one(
                "SELECT id FROM incidents WHERE zabbix_event_id=%s AND status NOT IN ('resolved','closed') "
                "ORDER BY id DESC LIMIT 1",
                (zabbix_id,),
            )
        elif wf.get("trigger_type") == "schedule":
            existing_incident = await fetch_one(
                "SELECT id FROM incidents WHERE title=%s AND owner_id=%s AND created_by='workflow' "
                "AND status NOT IN ('resolved','closed') "
                "AND started_at >= DATE_SUB(NOW(), INTERVAL 60 MINUTE) "
                "ORDER BY id DESC LIMIT 1",
                (title, owner_id),
            )
        if existing_incident:
            await execute(
                "UPDATE incidents SET description=%s WHERE id=%s",
                (ai_response[:2000], existing_incident["id"]),
            )
            runtime_ctx["incident_id"] = existing_incident["id"]
            runtime_ctx["incident_title"] = title
            return f"existing INC-{int(existing_incident['id']):04d}"
        inc_id = await execute(
            "INSERT INTO incidents "
            "(title, description, owner_id, severity, host, zabbix_event_id, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,'workflow')",
            (title, ai_response[:2000], owner_id, severity, host, zabbix_id),
        )
        runtime_ctx["incident_id"] = inc_id
        runtime_ctx["incident_title"] = title
        return f"created INC-{inc_id:04d}"

    elif action_type == "escalation":
        from datetime import datetime, timedelta

        escalated_to = str(cfg.get("escalated_to") or "Duty Manager").strip()[:200]
        if not escalated_to:
            return "error: no escalation target"
        channel = str(cfg.get("channel") or "teams").strip()[:50] or "teams"
        followup_minutes = max(5, min(240, int(cfg.get("followup_minutes") or 30)))
        max_followups = max(1, min(10, int(cfg.get("max_followups") or 3)))
        message_template = str(cfg.get("message") or "").strip()
        if message_template:
            message_sent = (
                message_template
                .replace("{workflow_name}", wf.get("name") or "Workflow")
                .replace("{ai_response}", ai_response[:500])
            )
        else:
            message_sent = ai_response[:2000]
        incident_id = runtime_ctx.get("incident_id") or cfg.get("incident_id") or trigger_data.get("incident_id")
        if incident_id == "":
            incident_id = None
        employee_id = cfg.get("employee_id") or wf.get("employee_id") or "aria"
        existing_escalation = None
        if incident_id:
            existing_escalation = await fetch_one(
                "SELECT id FROM escalations WHERE incident_id=%s AND employee_id=%s AND escalated_to=%s "
                "AND channel=%s AND status='open' ORDER BY id DESC LIMIT 1",
                (incident_id, employee_id, escalated_to, channel),
            )
        else:
            existing_escalation = await fetch_one(
                "SELECT id FROM escalations WHERE incident_id IS NULL AND employee_id=%s AND escalated_to=%s "
                "AND channel=%s AND status='open' "
                "AND created_at >= DATE_SUB(NOW(), INTERVAL 60 MINUTE) "
                "ORDER BY id DESC LIMIT 1",
                (employee_id, escalated_to, channel),
            )
        if existing_escalation:
            await execute(
                "UPDATE escalations SET message_sent=%s WHERE id=%s",
                (message_sent, existing_escalation["id"]),
            )
            runtime_ctx["escalation_id"] = existing_escalation["id"]
            return f"existing ESC-{int(existing_escalation['id']):04d}"
        esc_id = await execute(
            "INSERT INTO escalations "
            "(incident_id, employee_id, escalated_to, channel, message_sent, followup_at, max_followups) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                incident_id,
                employee_id,
                escalated_to,
                channel,
                message_sent,
                datetime.utcnow() + timedelta(minutes=followup_minutes),
                max_followups,
            ),
        )
        runtime_ctx["escalation_id"] = esc_id
        return f"created ESC-{esc_id:04d}"

    return f"unknown action type"


async def trigger_workflow_manually(workflow_id: int, requested_by: str = "system") -> dict:
    """Manually trigger a workflow. Returns run status."""
    return await _run_workflow(
        workflow_id,
        {"trigger": "manual", "ts": int(time.time())},
        requested_by=requested_by or "system",
    )


async def reload_scheduled_workflows():
    """Re-register all scheduled workflows (call after create/update/delete)."""
    global _scheduler
    sched = _scheduler
    if not sched or not sched.running:
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
