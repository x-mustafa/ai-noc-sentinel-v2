"""
Alert Rules Engine
Evaluate incoming Zabbix alarms against user-defined rules and fire actions.
"""
from __future__ import annotations

import logging
import json
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps import get_session, require_admin, require_operator
from app.database import fetch_all, fetch_one, execute

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Pydantic models ────────────────────────────────────────────────────────────

class AlertRuleBody(BaseModel):
    name: str
    condition_field: str          # e.g. "severity", "hostname", "name"
    condition_op: str             # eq, ne, gt, ge, lt, le, contains, not_contains
    condition_value: str
    action_type: str              # assign_employee, send_teams, send_email, suppress, create_incident
    action_data: Optional[str] = None   # JSON string with action-specific params
    cooldown_minutes: int = 60
    priority: int = 100
    enabled: bool = True


class AlertRuleUpdate(AlertRuleBody):
    pass


# ── CRUD endpoints ─────────────────────────────────────────────────────────────

@router.get("/alert-rules")
async def list_rules(session: dict = Depends(get_session)):
    rows = await fetch_all("SELECT * FROM alert_rules ORDER BY priority ASC, id ASC")
    return rows


@router.get("/alert-rules/{rule_id}")
async def get_rule(rule_id: int, session: dict = Depends(get_session)):
    row = await fetch_one("SELECT * FROM alert_rules WHERE id=%s", (rule_id,))
    if not row:
        raise HTTPException(404, "Rule not found")
    return row


@router.post("/alert-rules")
async def create_rule(body: AlertRuleBody, session: dict = Depends(require_admin)):
    _validate_rule(body)
    rule_id = await execute(
        """INSERT INTO alert_rules
           (name, condition_field, condition_op, condition_value,
            action_type, action_data, cooldown_minutes, priority, enabled)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (body.name, body.condition_field, body.condition_op, body.condition_value,
         body.action_type, body.action_data, body.cooldown_minutes,
         body.priority, int(body.enabled)),
    )
    return {"id": rule_id, "status": "created"}


@router.put("/alert-rules/{rule_id}")
async def update_rule(rule_id: int, body: AlertRuleUpdate, session: dict = Depends(require_admin)):
    existing = await fetch_one("SELECT id FROM alert_rules WHERE id=%s", (rule_id,))
    if not existing:
        raise HTTPException(404, "Rule not found")
    _validate_rule(body)
    await execute(
        """UPDATE alert_rules SET
           name=%s, condition_field=%s, condition_op=%s, condition_value=%s,
           action_type=%s, action_data=%s, cooldown_minutes=%s, priority=%s, enabled=%s
           WHERE id=%s""",
        (body.name, body.condition_field, body.condition_op, body.condition_value,
         body.action_type, body.action_data, body.cooldown_minutes,
         body.priority, int(body.enabled), rule_id),
    )
    return {"status": "updated"}


@router.patch("/alert-rules/{rule_id}/toggle")
async def toggle_rule(rule_id: int, session: dict = Depends(require_operator)):
    row = await fetch_one("SELECT id, enabled FROM alert_rules WHERE id=%s", (rule_id,))
    if not row:
        raise HTTPException(404, "Rule not found")
    new_val = 0 if row["enabled"] else 1
    await execute("UPDATE alert_rules SET enabled=%s WHERE id=%s", (new_val, rule_id))
    return {"enabled": bool(new_val)}


@router.delete("/alert-rules/{rule_id}")
async def delete_rule(rule_id: int, session: dict = Depends(require_admin)):
    await execute("DELETE FROM alert_rules WHERE id=%s", (rule_id,))
    return {"status": "deleted"}


# ── Validation ─────────────────────────────────────────────────────────────────

_VALID_OPS = {"eq", "ne", "gt", "ge", "lt", "le", "contains", "not_contains"}
_VALID_FIELDS = {"severity", "hostname", "name", "eventid", "host"}
_VALID_ACTIONS = {"assign_employee", "send_teams", "send_email", "suppress", "create_incident"}


def _validate_rule(body: AlertRuleBody):
    if body.condition_op not in _VALID_OPS:
        raise HTTPException(400, f"Invalid op: {body.condition_op}. Valid: {_VALID_OPS}")
    if body.action_type not in _VALID_ACTIONS:
        raise HTTPException(400, f"Invalid action: {body.action_type}. Valid: {_VALID_ACTIONS}")


# ── Engine ─────────────────────────────────────────────────────────────────────

async def evaluate_rules_for_alarm(alarm: dict):
    """
    Called by the workflow engine for each new Zabbix problem event.
    Evaluates all enabled alert rules in priority order (lower number = higher priority).
    Respects per-rule cooldown. Stops after first matching rule fires.
    """
    try:
        rules = await fetch_all(
            "SELECT * FROM alert_rules WHERE enabled=1 ORDER BY priority ASC, id ASC"
        )
        for rule in rules:
            if not _match_condition(
                rule["condition_field"],
                rule["condition_op"],
                rule["condition_value"],
                alarm,
            ):
                continue

            # Check cooldown
            last_fired = rule.get("last_fired")
            cooldown = int(rule.get("cooldown_minutes") or 60)
            if last_fired:
                if isinstance(last_fired, str):
                    last_fired = datetime.fromisoformat(last_fired)
                elapsed = (datetime.now(timezone.utc) - last_fired.replace(tzinfo=timezone.utc)).total_seconds() / 60
                if elapsed < cooldown:
                    logger.debug("Rule %s in cooldown (%.1f min remaining)", rule["id"], cooldown - elapsed)
                    continue

            # Fire action
            await _fire_action(rule, alarm)

            # Update fire stats
            await execute(
                "UPDATE alert_rules SET fire_count=fire_count+1, last_fired=NOW() WHERE id=%s",
                (rule["id"],),
            )

            if rule["action_type"] != "suppress":
                # Non-suppress rules: keep evaluating (multiple actions can fire)
                pass
            else:
                # Suppress stops evaluation
                break

    except Exception as exc:
        logger.error("evaluate_rules_for_alarm error: %s", exc)


def _match_condition(field: str, op: str, rule_val: str, alarm: dict) -> bool:
    """Pure condition evaluator — no I/O."""
    # Extract field value from alarm dict
    alarm_val_raw = alarm.get(field) or alarm.get("name") or ""
    if field == "hostname":
        # Try hosts list first
        hosts = alarm.get("hosts", [])
        alarm_val_raw = hosts[0].get("name", "") if hosts else alarm.get("host", "")
    alarm_val = str(alarm_val_raw).lower()
    rule_cmp  = str(rule_val).lower()

    try:
        if op == "eq":
            return alarm_val == rule_cmp
        if op == "ne":
            return alarm_val != rule_cmp
        if op == "contains":
            return rule_cmp in alarm_val
        if op == "not_contains":
            return rule_cmp not in alarm_val
        # Numeric comparisons
        a_num = float(alarm_val_raw)
        r_num = float(rule_val)
        if op == "gt":  return a_num > r_num
        if op == "ge":  return a_num >= r_num
        if op == "lt":  return a_num < r_num
        if op == "le":  return a_num <= r_num
    except (ValueError, TypeError):
        pass
    return False


async def _fire_action(rule: dict, alarm: dict):
    action = rule["action_type"]
    raw_data = rule.get("action_data") or "{}"
    try:
        data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
    except Exception:
        data = {}

    alarm_name = alarm.get("name", "Unknown alarm")
    severity    = alarm.get("severity", "?")
    host_str    = ""
    hosts = alarm.get("hosts", [])
    if hosts:
        host_str = hosts[0].get("name", "")

    if action == "suppress":
        logger.info("[AlertRule] SUPPRESS alarm: %s", alarm_name)
        return

    if action == "assign_employee":
        employee_id = data.get("employee_id", "cipher")
        try:
            from app.services.workflow_engine import _run_workflow_for_alarm
            import asyncio
            asyncio.create_task(_run_workflow_for_alarm(employee_id, alarm))
            logger.info("[AlertRule] Assigned alarm '%s' to employee %s", alarm_name, employee_id)
        except Exception as e:
            logger.error("[AlertRule] assign_employee failed: %s", e)
        return

    if action == "create_incident":
        try:
            sev_label = {0:"ok",1:"info",2:"warning",3:"average",4:"high",5:"critical"}.get(int(severity),"unknown")
            await execute(
                "INSERT INTO incidents (title, severity, status, source) VALUES (%s,%s,'open','alert_rule')",
                (f"[Auto] {alarm_name}" if not host_str else f"[Auto] {host_str}: {alarm_name}", sev_label),
            )
            logger.info("[AlertRule] Created incident for alarm: %s", alarm_name)
        except Exception as e:
            logger.error("[AlertRule] create_incident failed: %s", e)
        return

    if action == "send_teams":
        webhook_url = data.get("webhook_url", "")
        if not webhook_url:
            logger.warning("[AlertRule] send_teams: no webhook_url configured")
            return
        payload = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "summary": alarm_name,
            "themeColor": "FF0000" if int(severity or 0) >= 4 else "FFA500",
            "sections": [{
                "activityTitle": f"NOC Sentinel Alert: {alarm_name}",
                "facts": [
                    {"name": "Host", "value": host_str or "N/A"},
                    {"name": "Severity", "value": str(severity)},
                    {"name": "Rule", "value": rule["name"]},
                ],
            }],
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(webhook_url, json=payload)
            logger.info("[AlertRule] Teams webhook sent for: %s", alarm_name)
        except Exception as e:
            logger.error("[AlertRule] send_teams failed: %s", e)
        return

    if action == "send_email":
        to_addr = data.get("to", "")
        if not to_addr:
            logger.warning("[AlertRule] send_email: no 'to' configured")
            return
        try:
            from app.routers.ms365 import send_email_graph
            subject = f"NOC Alert: {alarm_name}"
            body_html = (
                f"<h3>NOC Sentinel Alert</h3>"
                f"<p><b>Alarm:</b> {alarm_name}</p>"
                f"<p><b>Host:</b> {host_str or 'N/A'}</p>"
                f"<p><b>Severity:</b> {severity}</p>"
                f"<p><b>Rule triggered:</b> {rule['name']}</p>"
            )
            await send_email_graph(to_addr, subject, body_html)
            logger.info("[AlertRule] Email sent to %s for alarm: %s", to_addr, alarm_name)
        except Exception as e:
            logger.error("[AlertRule] send_email failed: %s", e)
        return

    logger.warning("[AlertRule] Unknown action type: %s", action)
