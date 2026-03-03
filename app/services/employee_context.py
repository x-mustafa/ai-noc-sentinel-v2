"""
Employee operational context builders — Month 1 features (F1, F2, F3, F8).

Gathers real-time state (incidents owned, shift status, device knowledge)
and formats it for injection into AI employee system prompts.
Also exposes status helpers used by the workflow engine and incident router.
"""
import logging
from app.database import fetch_all, fetch_one, execute

logger = logging.getLogger(__name__)

_VALID_EMPLOYEES = {"aria", "nexus", "cipher", "vega"}


# ── F8 — Employee Status Helpers ──────────────────────────────────────────────

async def set_employee_status(
    employee_id: str,
    status: str,
    task: str = None,
) -> None:
    """Update an employee's status on the NOC board."""
    try:
        await execute(
            "UPDATE employee_profiles "
            "SET status=%s, current_task=%s, status_since=NOW() "
            "WHERE id=%s",
            (status, task[:490] if task else None, employee_id),
        )
    except Exception as e:
        logger.warning(f"set_employee_status({employee_id}, {status}) failed: {e}")


async def set_employee_investigating(employee_id: str, task_desc: str) -> None:
    await set_employee_status(employee_id, "investigating", task_desc)


async def set_employee_busy(employee_id: str, task_desc: str) -> None:
    await set_employee_status(employee_id, "busy", task_desc)


async def set_employee_available(employee_id: str) -> None:
    await set_employee_status(employee_id, "available", None)


# ── F2 — Incident Context ──────────────────────────────────────────────────────

async def get_employee_incident_context(employee_id: str) -> str:
    """Return formatted open incidents owned by the employee for prompt injection."""
    try:
        rows = await fetch_all(
            "SELECT id, title, status, severity, host, started_at "
            "FROM incidents "
            "WHERE owner_id=%s AND status NOT IN ('closed') "
            "ORDER BY severity DESC, started_at ASC LIMIT 10",
            (employee_id,),
        )
        if not rows:
            return ""

        lines = ["\n\n---- OPEN INCIDENTS YOU OWN ----"]
        for r in rows:
            started = str(r.get("started_at", ""))[:16]
            line = (
                f"  [INC-{r['id']:04d}] {r['title']} — {r['status'].upper()}"
                f" — sev {r['severity']}/5 — started {started}"
            )
            if r.get("host"):
                line += f" — host: {r['host']}"
            lines.append(line)

            # Inject last update
            last = await fetch_one(
                "SELECT update_text FROM incident_updates "
                "WHERE incident_id=%s ORDER BY created_at DESC LIMIT 1",
                (r["id"],),
            )
            if last and last.get("update_text"):
                txt = str(last["update_text"])[:150]
                lines.append(f'    Last update: "{txt}"')

        lines.append("---- END INCIDENTS ----")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"get_employee_incident_context failed: {e}")
        return ""


# ── F1 — Shift Context ────────────────────────────────────────────────────────

async def get_employee_shift_context(employee_id: str) -> str:
    """Return current shift state and last handover notes for prompt injection."""
    try:
        cfg = await fetch_one(
            "SELECT * FROM shift_config WHERE employee_id=%s",
            (employee_id,),
        )
        if not cfg or not cfg.get("enabled"):
            return ""

        lines = ["\n\n---- SHIFT CONTEXT ----"]
        lines.append(
            f"  Schedule: {cfg.get('shift_start','07:00')} – "
            f"{cfg.get('shift_end','15:00')} ({cfg.get('timezone','Asia/Baghdad')})"
        )

        # Most recent handover
        last_ho = await fetch_one(
            "SELECT * FROM shift_handover WHERE employee_id=%s "
            "ORDER BY created_at DESC LIMIT 1",
            (employee_id,),
        )
        if last_ho:
            lines.append(f"  Last shift status: {last_ho.get('status', 'unknown')}")
            if last_ho.get("briefing"):
                briefing_snippet = str(last_ho["briefing"])[:500]
                lines.append(f"  Handover briefing:\n{briefing_snippet}")
            if last_ho.get("watch_items"):
                lines.append(f"  Watch items: {last_ho['watch_items']}")

        lines.append("---- END SHIFT ----")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"get_employee_shift_context failed: {e}")
        return ""


# ── F3 — Device Knowledge Context ─────────────────────────────────────────────

async def get_employee_device_knowledge(employee_id: str, host: str = None) -> str:
    """Return device knowledge for a specific host for prompt injection."""
    if not host:
        return ""
    try:
        rows = await fetch_all(
            "SELECT category, note, confidence, verified FROM device_knowledge "
            "WHERE employee_id=%s AND host=%s "
            "ORDER BY verified DESC, confidence DESC LIMIT 10",
            (employee_id, host),
        )
        if not rows:
            return ""

        lines = [f"\n\n---- YOUR DEVICE KNOWLEDGE — {host} ----"]
        for r in rows:
            cat = str(r.get("category", "note")).upper()
            verified = " [VERIFIED]" if r.get("verified") else ""
            conf = r.get("confidence", 3)
            lines.append(f"  [{cat}{verified}] (conf {conf}/5) {r['note']}")
        lines.append("---- END DEVICE KNOWLEDGE ----")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"get_employee_device_knowledge failed: {e}")
        return ""


# ── Combined Context ───────────────────────────────────────────────────────────

# ── F4 — Peer Inbox Context ───────────────────────────────────────────────────

async def get_employee_inbox_context(employee_id: str) -> str:
    """Return pending peer messages for prompt injection (max 5)."""
    try:
        rows = await fetch_all(
            "SELECT id, from_employee, subject, body FROM employee_messages "
            "WHERE to_employee=%s AND status='pending' ORDER BY created_at ASC LIMIT 5",
            (employee_id,),
        )
        if not rows:
            return ""

        lines = [f"\n\n---- INBOX — PENDING PEER MESSAGES ({len(rows)}) ----"]
        for r in rows:
            lines.append(
                f"  [MSG-{r['id']}] FROM {r['from_employee'].upper()}: "
                f"{r.get('subject') or '(no subject)'}"
            )
            snippet = str(r.get("body", ""))[:200]
            lines.append(f"    {snippet}")
        lines.append("  Consider these when relevant to your current task.")
        lines.append("---- END INBOX ----")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"get_employee_inbox_context failed: {e}")
        return ""


# ── F5 — Performance Context ──────────────────────────────────────────────────

async def get_employee_performance_context(employee_id: str) -> str:
    """Return accuracy stats for injection into employee prompt (only when data exists)."""
    try:
        rows = await fetch_all(
            "SELECT task_type, domain, correct_count, total_count FROM employee_performance "
            "WHERE employee_id=%s AND total_count >= 3 ORDER BY total_count DESC LIMIT 8",
            (employee_id,),
        )
        if not rows:
            return ""

        lines = ["\n\n---- YOUR PERFORMANCE CONTEXT ----"]
        for r in rows:
            total = r.get("total_count", 0)
            pct   = round(r["correct_count"] / total * 100) if total else 0
            lines.append(
                f"  {r.get('domain','general')} ({r.get('task_type','?')}): "
                f"{r['correct_count']}/{total} correct ({pct}%)"
            )
        lines.append("  Use this to calibrate confidence in your current assessment.")
        lines.append("---- END PERFORMANCE ----")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"get_employee_performance_context failed: {e}")
        return ""


# ── F14 — SLA Context (VEGA only) ─────────────────────────────────────────────

async def get_sla_context() -> str:
    """Return current-month SLA status for VEGA's prompt injection."""
    try:
        import datetime
        month = datetime.date.today().replace(day=1).isoformat()
        rows  = await fetch_all(
            "SELECT service, target_sla, downtime_min FROM sla_tracker "
            "WHERE month=%s ORDER BY service",
            (month,),
        )
        if not rows:
            return ""

        MINUTES_IN_MONTH = 30 * 24 * 60
        lines = ["\n\n---- SLA STATUS — CURRENT MONTH ----"]
        for r in rows:
            target   = float(r.get("target_sla", 99.99))
            downtime = int(r.get("downtime_min", 0))
            uptime   = round((MINUTES_IN_MONTH - downtime) / MINUTES_IN_MONTH * 100, 4)
            budget   = round((100 - target) / 100 * MINUTES_IN_MONTH, 1)
            used_pct = round(downtime / budget * 100, 1) if budget > 0 else 0
            flags    = ""
            if uptime < target:
                flags += " [BREACHED]"
            elif used_pct >= 80:
                flags += " [AT RISK]"
            lines.append(
                f"  {r['service']}: {uptime}% uptime (target {target}%) — "
                f"{downtime}min down — budget {used_pct}% used{flags}"
            )
        lines.append("---- END SLA ----")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"get_sla_context failed: {e}")
        return ""


# ── Combined Context ───────────────────────────────────────────────────────────

async def get_full_operational_context(employee_id: str, host: str = None, alarm_type: str = None) -> str:
    """
    Combine all real-time operational context into one block for system prompt injection.
    Includes: open incidents, shift state, inbox messages, performance stats,
    device knowledge (if host provided), SLA status (VEGA only),
    F11 time-based patterns, F13 active change windows.
    """
    import asyncio
    from app.services.memory import get_pattern_context
    from app.routers.changes import get_active_change_context

    (
        incident_ctx,
        shift_ctx,
        inbox_ctx,
        performance_ctx,
        device_ctx,
        sla_ctx,
        pattern_ctx,
        change_ctx,
    ) = await asyncio.gather(
        get_employee_incident_context(employee_id),
        get_employee_shift_context(employee_id),
        get_employee_inbox_context(employee_id),
        get_employee_performance_context(employee_id),
        get_employee_device_knowledge(employee_id, host) if host else _noop(),
        get_sla_context() if employee_id == "vega" else _noop(),
        get_pattern_context(employee_id, host=host, alarm_type=alarm_type),
        get_active_change_context(),
    )

    parts = [incident_ctx, shift_ctx, inbox_ctx, performance_ctx, device_ctx, sla_ctx]
    if pattern_ctx:
        parts.append(f"\n\n---- TIME-BASED PATTERNS ----\n{pattern_ctx}\n---- END PATTERNS ----")
    if change_ctx:
        parts.append(f"\n\n---- {change_ctx} ----")
    return "".join(parts)


async def _noop() -> str:
    return ""
