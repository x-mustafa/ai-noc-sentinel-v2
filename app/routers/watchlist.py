"""
F7 — Proactive Trend Watch
Employees maintain a watchlist of hosts/metrics they monitor between alarms.
Background scan runs every 4 hours and triggers AI analysis on anything trending.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.deps import get_session, require_operator
from app.database import fetch_all, fetch_one, execute

router = APIRouter()


class WatchlistEntry(BaseModel):
    employee_id: str
    host: Optional[str] = None
    metric_key: Optional[str] = None
    watch_reason: Optional[str] = None
    threshold_pct: int = 80
    added_from: str = "manual"


class WatchlistScanResult(BaseModel):
    host: str
    metric_key: Optional[str]
    finding: str
    severity: str  # info / warning / critical


# ── CRUD ──────────────────────────────────────────────────────────────────────

@router.get("/watchlist/{employee_id}")
async def get_watchlist(employee_id: str, session: dict = Depends(get_session)):
    rows = await fetch_all(
        "SELECT * FROM watchlist WHERE employee_id=%s AND is_active=1 ORDER BY created_at DESC",
        (employee_id,),
    )
    return rows


@router.post("/watchlist")
async def add_to_watchlist(body: WatchlistEntry, session: dict = Depends(require_operator)):
    if not body.host and not body.metric_key:
        raise HTTPException(400, "Provide at least host or metric_key")
    row_id = await execute(
        "INSERT INTO watchlist (employee_id, host, metric_key, watch_reason, threshold_pct, added_from) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (body.employee_id, body.host, body.metric_key,
         body.watch_reason, body.threshold_pct, body.added_from),
    )
    return {"id": row_id, "status": "added"}


@router.delete("/watchlist/{entry_id}")
async def remove_from_watchlist(entry_id: int, session: dict = Depends(require_operator)):
    await execute("UPDATE watchlist SET is_active=0 WHERE id=%s", (entry_id,))
    return {"status": "removed"}


@router.post("/watchlist/{entry_id}/scan")
async def trigger_watchlist_scan(entry_id: int, session: dict = Depends(require_operator)):
    """Manually trigger a scan for one watchlist entry."""
    entry = await fetch_one("SELECT * FROM watchlist WHERE id=%s AND is_active=1", (entry_id,))
    if not entry:
        raise HTTPException(404, "Watchlist entry not found")
    result = await _scan_entry(entry)
    return result


@router.post("/watchlist/{employee_id}/scan-all")
async def trigger_full_scan(employee_id: str, session: dict = Depends(require_operator)):
    """Manually trigger a scan for all watchlist entries of an employee."""
    results = await _scan_employee_watchlist(employee_id)
    return {"scanned": len(results), "findings": results}


# ── Background scan logic ─────────────────────────────────────────────────────

async def _scan_entry(entry: dict) -> dict:
    """
    Pull Zabbix data for the watched host/metric and ask the employee's AI
    to assess if anything is trending toward the threshold.
    Returns a finding dict.
    """
    import json
    from app.database import fetch_one as db_fetch
    from app.services.ai_stream import stream_ai

    host       = entry.get("host", "")
    metric_key = entry.get("metric_key", "")
    emp_id     = entry["employee_id"]
    threshold  = entry.get("threshold_pct", 80)

    # Pull Zabbix history for this item (last 4h, 10 datapoints)
    zabbix_data = ""
    try:
        cfg = await db_fetch("SELECT * FROM zabbix_config LIMIT 1")
        if cfg:
            from app.services.zabbix_client import call_zabbix
            # Find item key
            items = await call_zabbix(cfg, "item.get", {
                "output": ["itemid", "name", "lastvalue", "units"],
                "host": host,
                "search": {"key_": metric_key} if metric_key else {},
                "limit": 5,
            })
            if items:
                item = items[0]
                history = await call_zabbix(cfg, "history.get", {
                    "itemids": [item["itemid"]],
                    "sortfield": "clock",
                    "sortorder": "DESC",
                    "limit": 10,
                    "output": "extend",
                })
                values = [h["value"] for h in history]
                zabbix_data = (
                    f"Item: {item['name']} (key: {metric_key})\n"
                    f"Last 10 values: {', '.join(values)}\n"
                    f"Latest: {item['lastvalue']} {item.get('units','')}"
                )
    except Exception:
        zabbix_data = "(Zabbix data unavailable — assess from general knowledge)"

    # Build assessment prompt
    watch_reason = entry.get("watch_reason") or "general monitoring"
    prompt = (
        f"WATCHLIST SCAN — {host or 'N/A'}\n\n"
        f"Watch reason: {watch_reason}\n"
        f"Alert threshold: {threshold}%\n\n"
        f"Current data:\n{zabbix_data or '(no metric data available)'}\n\n"
        f"Is this metric trending toward the {threshold}% threshold? "
        f"Is there any cause for concern right now?\n\n"
        f"Reply with:\n"
        f"SEVERITY: info/warning/critical\n"
        f"FINDING: one sentence\n"
        f"ACTION: what (if anything) should be done"
    )

    finding_text = ""
    severity     = "info"
    try:
        cfg = await db_fetch("SELECT * FROM zabbix_config LIMIT 1")
        if cfg:
            from app.services.employee_prompt import build_employee_system_prompt
            sys_prompt = await build_employee_system_prompt(emp_id)
            chunks = []
            async for chunk in stream_ai(
                cfg.get("provider", "claude"), cfg.get("claude_key", ""),
                cfg.get("model", "claude-haiku-4-5-20251001"),
                sys_prompt, prompt,
            ):
                if chunk.get("type") == "text":
                    chunks.append(chunk["text"])
            finding_text = "".join(chunks).strip()
            if "critical" in finding_text.lower():
                severity = "critical"
            elif "warning" in finding_text.lower():
                severity = "warning"
    except Exception as e:
        finding_text = f"(scan error: {e})"

    # Update last_checked timestamp
    await execute("UPDATE watchlist SET last_checked=NOW() WHERE id=%s", (entry["id"],))

    # If warning/critical → save to employee memory so it informs future responses
    if severity in ("warning", "critical"):
        try:
            from app.services.memory import save_memory_direct
            await save_memory_direct(
                emp_id,
                task_type="watchlist_scan",
                task_summary=f"Watchlist scan: {host} — {severity}",
                outcome_summary=finding_text[:500],
                key_learnings=f"Host {host} trending toward threshold. Requires attention.",
            )
        except Exception:
            pass

    return {
        "watchlist_id": entry["id"],
        "host": host,
        "metric_key": metric_key,
        "severity": severity,
        "finding": finding_text,
    }


async def _scan_employee_watchlist(employee_id: str) -> list[dict]:
    """Scan all active watchlist entries for one employee."""
    import asyncio
    entries = await fetch_all(
        "SELECT * FROM watchlist WHERE employee_id=%s AND is_active=1",
        (employee_id,),
    )
    if not entries:
        return []
    tasks   = [_scan_entry(e) for e in entries]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]


async def run_watchlist_scan_all():
    """APScheduler job: scan all employees' watchlists every 4 hours."""
    import logging
    log = logging.getLogger(__name__)
    log.info("[WATCHLIST] Starting scheduled scan for all employees...")
    for emp_id in ("aria", "nexus", "cipher", "vega"):
        try:
            findings = await _scan_employee_watchlist(emp_id)
            critical = [f for f in findings if f.get("severity") == "critical"]
            warnings = [f for f in findings if f.get("severity") == "warning"]
            log.info(
                f"[WATCHLIST] {emp_id.upper()}: {len(findings)} scanned, "
                f"{len(critical)} critical, {len(warnings)} warnings"
            )
        except Exception as e:
            log.error(f"[WATCHLIST] {emp_id} scan error: {e}")
