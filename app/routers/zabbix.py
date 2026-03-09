import time
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from app.deps import get_session, require_admin, require_operator
from app.services.zabbix_client import call_zabbix, get_zabbix_config
from app.database import fetch_all, fetch_one, execute

router = APIRouter()


@router.get("/status")
async def status(
    hostids: List[str] = Query(default=[]),
    session: dict = Depends(get_session),
):
    db_rows = await fetch_all(
        "SELECT DISTINCT zabbix_host_id FROM map_nodes "
        "WHERE zabbix_host_id IS NOT NULL AND zabbix_host_id != ''"
    )
    db_host_ids = [r["zabbix_host_id"] for r in db_rows]
    map_host_ids = list(set(list(hostids) + db_host_ids))

    host_params = {
        "output": ["hostid", "host", "name", "status", "description"],
        "selectInterfaces": ["ip", "main", "type", "available"],
        "monitored_hosts": 1,
    }
    if map_host_ids:
        host_params["hostids"] = map_host_ids

    hosts_raw = await call_zabbix("host.get", host_params)
    hosts = hosts_raw if isinstance(hosts_raw, list) else []

    prob_params = {
        "output": ["eventid", "objectid", "name", "severity", "clock", "acknowledged"],
        "selectAcknowledges": ["clock", "message", "userid"],
        "sortfield": "eventid", "sortorder": "DESC", "limit": 1000,
    }
    if map_host_ids:
        prob_params["hostids"] = map_host_ids

    problems_raw = await call_zabbix("problem.get", prob_params)
    problems = problems_raw if isinstance(problems_raw, list) else []

    tids = list(set(p["objectid"] for p in problems if isinstance(p, dict) and "objectid" in p))
    trigger_host_map: dict = {}
    if tids:
        trigs_raw = await call_zabbix("trigger.get", {
            "output": ["triggerid", "priority", "description"],
            "selectHosts": ["hostid", "host", "name"],
            "triggerids": tids,
        })
        for t in (trigs_raw or []):
            for h in t.get("hosts", []):
                trigger_host_map.setdefault(t["triggerid"], []).append(h["hostid"])

    host_problems: dict = {}
    for p in problems:
        if not isinstance(p, dict):
            continue
        hids = trigger_host_map.get(p.get("objectid"), [])
        p["host_ids"] = hids
        for hid in hids:
            host_problems.setdefault(hid, []).append({
                "eventid":      p["eventid"],
                "name":         p["name"],
                "severity":     int(p.get("severity", 0)),
                "clock":        int(p.get("clock", 0)),
                "acknowledged": p.get("acknowledged", "0"),
            })

    for h in hosts:
        hid = h["hostid"]
        probs = host_problems.get(hid, [])
        h["problems"]       = probs
        h["problem_count"]  = len(probs)
        h["worst_severity"] = max((p["severity"] for p in probs), default=0)
        ip, available = "", 0
        for iface in h.get("interfaces", []):
            if str(iface.get("main")) == "1":
                ip        = iface.get("ip", "")
                available = int(iface.get("available", 0))
                break
        h["ip"]        = ip
        h["available"] = available
        h.pop("interfaces", None)

    return {
        "hosts":    hosts,
        "problems": problems,
        "counts": {
            "total":         len(hosts),
            "ok":            sum(1 for h in hosts if h["problem_count"] == 0 and h["available"] == 1),
            "with_problems": sum(1 for h in hosts if h["problem_count"] > 0),
            "unavailable":   sum(1 for h in hosts if h["available"] == 2),
            "alarms":        len(problems),
        },
        "ts": int(time.time()),
    }


@router.get("/problems")
async def problems(
    severity: Optional[int] = Query(default=None),
    hostid: Optional[str]   = Query(default=None),
    hostids: List[str]       = Query(default=[]),
    session: dict = Depends(get_session),
):
    db_rows = await fetch_all(
        "SELECT DISTINCT zabbix_host_id FROM map_nodes "
        "WHERE zabbix_host_id IS NOT NULL AND zabbix_host_id != ''"
    )
    db_host_ids = [r["zabbix_host_id"] for r in db_rows]
    map_host_ids = list(set(list(hostids) + db_host_ids))

    params: dict = {
        "output": "extend", "selectAcknowledges": "extend",
        "sortfield": "eventid", "sortorder": "DESC", "limit": 500,
    }
    if severity is not None:
        params["severities"] = [severity]
    if hostid:
        params["hostids"] = [hostid]
    elif map_host_ids:
        params["hostids"] = map_host_ids

    probs_raw = await call_zabbix("problem.get", params)
    probs = probs_raw if isinstance(probs_raw, list) else []

    tids = list(set(p["objectid"] for p in probs if isinstance(p, dict)))
    trig_map: dict = {}
    if tids:
        trigs_raw = await call_zabbix("trigger.get", {
            "output": ["triggerid", "priority", "description"],
            "selectHosts": ["hostid", "host", "name"],
            "triggerids": tids,
        })
        for t in (trigs_raw or []):
            trig_map[t["triggerid"]] = t

    for p in probs:
        if not isinstance(p, dict):
            continue
        t = trig_map.get(p.get("objectid"))
        p["trigger_desc"] = (t or {}).get("description", p.get("name"))
        p["hosts"]        = (t or {}).get("hosts", [])
        p["priority"]     = int((t or {}).get("priority", p.get("severity", 0)))

    return probs


@router.get("/traffic")
async def traffic(
    hosts: List[str] = Query(default=[]),
    session: dict = Depends(get_session),
):
    if not hosts:
        return {}
    items_raw = await call_zabbix("item.get", {
        "output": ["hostid", "key_", "lastvalue"],
        "hostids": hosts,
        "search": {"key_": "net.if"},
        "searchWildcardsEnabled": True,
        "monitored": True,
        "limit": 500,
    })
    result: dict = {}
    for item in (items_raw or []):
        if not isinstance(item, dict):
            continue
        hid = item["hostid"]
        key = item.get("key_", "")
        val = float(item.get("lastvalue", 0) or 0)
        if hid not in result:
            result[hid] = {"in": 0.0, "out": 0.0}
        if ".in[" in key:
            result[hid]["in"]  += val
        if ".out[" in key:
            result[hid]["out"] += val
    return result


@router.get("/history")
async def history(
    hostid: str = Query(...),
    session: dict = Depends(get_session),
):
    now  = int(time.time())
    frm  = now - 3600
    slot_patterns = {
        "cpu": ["system.cpu.util", "system.cpu.load[percpu,avg1]", "system.cpu.load"],
        "mem": ["vm.memory.size[pused]", "vm.memory.utilization", "vm.memory.size[available]"],
        "net": ["net.if.in", "net.if.out"],
    }
    result = []
    for slot, patterns in slot_patterns.items():
        item = None
        for pat in patterns:
            raw = await call_zabbix("item.get", {
                "output": ["itemid", "key_", "name", "value_type", "units", "lastvalue"],
                "hostids": [hostid],
                "search": {"key_": pat},
                "searchWildcardsEnabled": True,
                "monitored": True,
                "limit": 1,
            })
            if isinstance(raw, list) and raw:
                item = raw[0]
                break
        if not item:
            continue
        history_raw = await call_zabbix("history.get", {
            "output": ["clock", "value"],
            "history": int(item.get("value_type", 0)),
            "itemids": [item["itemid"]],
            "time_from": frm, "time_till": now,
            "limit": 120,
            "sortfield": "clock", "sortorder": "ASC",
        })
        pts = [{"t": int(h["clock"]), "v": float(h["value"])}
               for h in (history_raw or []) if isinstance(h, dict)]
        result.append({
            "slot":      slot,
            "name":      item["name"],
            "units":     item.get("units", ""),
            "lastvalue": item.get("lastvalue", "0"),
            "history":   pts,
        })
    return result


class AckBody(BaseModel):
    eventid: str
    message: str = "Acknowledged via Tabadul NOC"


@router.post("/acknowledge")
async def acknowledge(body: AckBody, session: dict = Depends(require_operator)):
    result = await call_zabbix("event.acknowledge", {
        "eventids": [body.eventid],
        "action": 6,
        "message": body.message,
    })
    return {"ok": True, "result": result}


@router.post("/test")
async def test_connection(session: dict = Depends(get_session)):
    result = await call_zabbix("apiinfo.version", {})
    if result is None:
        return {"ok": False, "error": "Cannot reach Zabbix server"}
    if isinstance(result, dict) and "_zabbix_error" in result:
        return {"ok": False, "error": result["_zabbix_error"]}
    return {"ok": True, "version": result}


@router.get("/config")
async def get_config(session: dict = Depends(get_session)):
    cfg = await get_zabbix_config()
    token = cfg.get("token", "")
    cfg["token_masked"] = (token[:8] + "*" * 32 + token[-4:]) if len(token) > 12 else "****"
    return cfg


class ZabbixConfigBody(BaseModel):
    url: str
    token: str
    refresh: int = 30


@router.put("/config")
async def save_config(body: ZabbixConfigBody, session: dict = Depends(require_admin)):
    existing = await fetch_one("SELECT COUNT(*) as c FROM zabbix_config")
    token_changed = "*" not in body.token
    if existing and existing["c"]:
        if token_changed:
            await execute("UPDATE zabbix_config SET url=%s, token=%s, refresh=%s",
                          (body.url, body.token, body.refresh))
        else:
            await execute("UPDATE zabbix_config SET url=%s, refresh=%s",
                          (body.url, body.refresh))
    else:
        await execute("INSERT INTO zabbix_config (url, token, refresh) VALUES (%s,%s,%s)",
                      (body.url, body.token, body.refresh))
    return {"ok": True}


# ── Script / Command Execution ─────────────────────────────────────────────────

class ScriptExecBody(BaseModel):
    scriptid: str
    hostid: str
    confirm: bool = False   # explicit safety gate — must be True to execute


@router.get("/scripts")
async def list_scripts(
    hostid: Optional[str] = Query(default=None),
    session: dict = Depends(require_operator),
):
    """List Zabbix global scripts available for a host (or all scripts)."""
    params: dict = {"output": ["scriptid", "name", "description", "type", "scope", "command"]}
    if hostid:
        params["hostids"] = [hostid]
    result = await call_zabbix("script.get", params)
    if not isinstance(result, list):
        return []
    return result


@router.post("/scripts/execute")
async def execute_script(body: ScriptExecBody, session: dict = Depends(require_operator)):
    """
    Execute a Zabbix global script on a host.
    Requires confirm=true to prevent accidental execution.
    Result is returned verbatim from Zabbix.
    """
    if not body.confirm:
        raise HTTPException(400, "Set confirm=true to execute the script")

    result = await call_zabbix("script.execute", {
        "scriptid": body.scriptid,
        "hostid":   body.hostid,
    })
    if isinstance(result, dict) and result.get("_zabbix_error"):
        raise HTTPException(502, result["_zabbix_error"])
    return {"ok": True, "result": result}


# ── Multi-site management ──────────────────────────────────────────────────────

class SiteBody(BaseModel):
    name: str
    url: str
    token: Optional[str] = ""
    color: Optional[str] = "#00d4ff"
    enabled: bool = True
    is_default: bool = False
    notes: Optional[str] = None


@router.get("/sites")
async def list_sites(session: dict = Depends(get_session)):
    """Return all configured Zabbix sites (tokens masked)."""
    rows = await fetch_all("SELECT * FROM sites ORDER BY is_default DESC, name ASC")
    for r in rows:
        t = r.get("token") or ""
        r["token_masked"] = (t[:4] + "***" + t[-4:]) if len(t) > 8 else ("***" if t else "")
        r.pop("token", None)
        r.pop("password", None)
    return rows


@router.post("/sites")
async def create_site(body: SiteBody, session: dict = Depends(require_admin)):
    if body.is_default:
        await execute("UPDATE sites SET is_default=0")
    site_id = await execute(
        "INSERT INTO sites (name, url, token, color, enabled, is_default, notes) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (body.name, body.url, body.token or "", body.color, int(body.enabled), int(body.is_default), body.notes),
    )
    return {"id": site_id, "status": "created"}


@router.put("/sites/{site_id}")
async def update_site(site_id: int, body: SiteBody, session: dict = Depends(require_admin)):
    existing = await fetch_one("SELECT id FROM sites WHERE id=%s", (site_id,))
    if not existing:
        raise HTTPException(404, "Site not found")
    if body.is_default:
        await execute("UPDATE sites SET is_default=0")
    token_changed = body.token and "*" not in body.token
    if token_changed:
        await execute(
            "UPDATE sites SET name=%s, url=%s, token=%s, color=%s, enabled=%s, is_default=%s, notes=%s WHERE id=%s",
            (body.name, body.url, body.token, body.color, int(body.enabled), int(body.is_default), body.notes, site_id),
        )
    else:
        await execute(
            "UPDATE sites SET name=%s, url=%s, color=%s, enabled=%s, is_default=%s, notes=%s WHERE id=%s",
            (body.name, body.url, body.color, int(body.enabled), int(body.is_default), body.notes, site_id),
        )
    return {"status": "updated"}


@router.delete("/sites/{site_id}")
async def delete_site(site_id: int, session: dict = Depends(require_admin)):
    await execute("DELETE FROM sites WHERE id=%s", (site_id,))
    return {"status": "deleted"}


@router.post("/sites/{site_id}/test")
async def test_site(site_id: int, session: dict = Depends(get_session)):
    """Test connectivity to a specific site."""
    row = await fetch_one("SELECT * FROM sites WHERE id=%s", (site_id,))
    if not row:
        raise HTTPException(404, "Site not found")
    from app.services.zabbix_client import call_zabbix as _call
    cfg = {"url": row["url"], "token": row["token"], "username": row.get("username",""), "password": row.get("password","")}
    result = await _call("apiinfo.version", {}, cfg_override=cfg)
    if result is None or (isinstance(result, dict) and "_zabbix_error" in result):
        return {"ok": False, "error": str(result)}
    return {"ok": True, "version": result, "site": row["name"]}


@router.get("/summary")
async def zabbix_summary(session: dict = Depends(get_session)):
    """Compact summary for the monitoring dashboard."""
    problems_raw = await call_zabbix("problem.get", {
        "output": ["eventid", "name", "severity", "clock", "objectid"],
        "recent": True, "sortfield": "eventid", "sortorder": "DESC", "limit": 200,
    })
    problems = problems_raw if isinstance(problems_raw, list) else []
    hosts_raw = await call_zabbix("host.get", {
        "output": ["hostid", "available"],
        "monitored_hosts": 1, "filter": {"status": 0},
    })
    hosts = hosts_raw if isinstance(hosts_raw, list) else []
    sev = {0:0, 1:0, 2:0, 3:0, 4:0, 5:0}
    for p in problems:
        s = int(p.get("severity", 0))
        sev[s] = sev.get(s, 0) + 1
    return {
        "total_hosts": len(hosts),
        "total_problems": len(problems),
        "severity_counts": sev,
        "top_problems": [
            {"name": p.get("name",""), "severity": int(p.get("severity",0)), "clock": int(p.get("clock",0))}
            for p in problems[:10]
        ],
        "ok": not (isinstance(problems_raw, dict) and "_zabbix_error" in problems_raw),
    }
