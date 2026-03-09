from urllib.parse import urlparse, urljoin

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.database import execute, fetch_one
from app.deps import get_session, require_admin, require_operator
from app.services.observability import (
    build_monitoring_overview,
    collect_monitoring_snapshot,
    configured_dashboard_hosts,
    credentials_for_url,
    preflight_dashboard_url,
    sync_kuma_override,
    summarize_monitoring_snapshot,
    target_settings,
    target_settings_for_ui,
)

router = APIRouter()


def _allowed_target(hostname: str, request_host: str, cfg: dict | None = None) -> bool:
    host = (hostname or "").strip().lower().strip("[]")
    req_host = (request_host or "").split(":")[0].strip().lower()
    if not host:
        return False
    if req_host and host == req_host:
        return True
    if host in configured_dashboard_hosts(cfg):
        return True
    if host == "tabadul.iq" or host.endswith(".tabadul.iq"):
        return True
    return False


class ObservabilityConfigBody(BaseModel):
    grafana_url: str | None = Field(None, max_length=2000)
    grafana_username: str | None = Field(None, max_length=200)
    grafana_password: str | None = Field(None, max_length=500)
    grafana_payment_dashboard_url: str | None = Field(None, max_length=2000)
    zabbix_web_url: str | None = Field(None, max_length=2000)
    zabbix_web_username: str | None = Field(None, max_length=200)
    zabbix_web_password: str | None = Field(None, max_length=500)
    kuma_url: str | None = Field(None, max_length=2000)
    kuma_public_url: str | None = Field(None, max_length=2000)
    kuma_app_url: str | None = Field(None, max_length=2000)
    kuma_sync_url: str | None = Field(None, max_length=2000)
    kuma_username: str | None = Field(None, max_length=200)
    kuma_password: str | None = Field(None, max_length=500)
    auto_monitor_enabled: bool | None = None
    monitor_interval_minutes: int | None = Field(None, ge=1, le=60)


def _normalize_url(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(400, "Observability URLs must start with http:// or https://")
    if parsed.username or parsed.password:
        raise HTTPException(400, "Do not embed credentials in dashboard URLs")
    return raw


async def _load_cfg() -> dict:
    return await fetch_one("SELECT * FROM zabbix_config LIMIT 1") or {}


async def _upsert_autonomous_patrols(cfg: dict, requested_by: str) -> list[dict]:
    from app.services.workflow_engine import reload_scheduled_workflows, trigger_workflow_manually

    interval = int(cfg.get("observability_monitor_interval_minutes") or 5)
    interval = max(1, min(60, interval))
    cron = "0 * * * *" if interval >= 60 else f"*/{interval} * * * *"

    patrols = [
        {
            "employee_id": "aria",
            "name": "Autonomous Patrol - ARIA",
            "prompt": (
                "Review the live observability snapshot and current NOC context. "
                "If everything looks healthy, respond with exactly 'STATUS: NORMAL' plus one short sentence. "
                "If anything is abnormal, begin with 'STATUS: ABNORMAL', list the concrete symptoms, likely impact, "
                "the next operator action, and whether an incident should be raised."
            ),
            "action_type": '["log","incident","escalation"]',
            "action_config": '{"incident":{"title":"Autonomous Patrol: {workflow_name}","severity":3,"owner_id":"aria"},"escalation":{"employee_id":"aria","escalated_to":"Duty Manager","channel":"teams","followup_minutes":20,"max_followups":3}}',
            "risk_tier": "safe_auto",
        },
        {
            "employee_id": "nexus",
            "name": "Autonomous Patrol - NEXUS",
            "prompt": (
                "Review the live observability snapshot with an infrastructure lens. "
                "If healthy, start with 'STATUS: NORMAL'. "
                "If abnormal, start with 'STATUS: ABNORMAL' and explain likely infrastructure root causes, "
                "containment steps, and the most probable owner."
            ),
            "action_type": '["log","incident","escalation"]',
            "action_config": '{"incident":{"title":"Infra Patrol: {workflow_name}","severity":3,"owner_id":"nexus"},"escalation":{"employee_id":"nexus","escalated_to":"Infrastructure Lead","channel":"teams","followup_minutes":20,"max_followups":3}}',
            "risk_tier": "safe_auto",
        },
        {
            "employee_id": "cipher",
            "name": "Autonomous Patrol - CIPHER",
            "prompt": (
                "Review the live observability snapshot for security or access anomalies. "
                "If healthy, start with 'STATUS: NORMAL'. "
                "If abnormal, start with 'STATUS: ABNORMAL' and state the security concern, exposure, "
                "recommended containment, and escalation urgency."
            ),
            "action_type": '["log","incident","escalation"]',
            "action_config": '{"incident":{"title":"Security Patrol: {workflow_name}","severity":2,"owner_id":"cipher"},"escalation":{"employee_id":"cipher","escalated_to":"Security Lead","channel":"teams","followup_minutes":15,"max_followups":4}}',
            "risk_tier": "approval_required",
        },
        {
            "employee_id": "vega",
            "name": "Autonomous Patrol - VEGA",
            "prompt": (
                "Review the live observability snapshot for reliability and coverage gaps. "
                "If healthy, start with 'STATUS: NORMAL'. "
                "If abnormal, start with 'STATUS: ABNORMAL' and explain the reliability risk, "
                "user impact, immediate mitigation, and monitoring gap if any."
            ),
            "action_type": '["log","incident","escalation"]',
            "action_config": '{"incident":{"title":"Reliability Patrol: {workflow_name}","severity":3,"owner_id":"vega"},"escalation":{"employee_id":"vega","escalated_to":"SRE Lead","channel":"teams","followup_minutes":20,"max_followups":3}}',
            "risk_tier": "safe_auto",
        },
    ]

    results: list[dict] = []
    for patrol in patrols:
        existing = await fetch_one("SELECT id FROM workflows WHERE name=%s", (patrol["name"],))
        if existing:
            wf_id = int(existing["id"])
            await execute(
                "UPDATE workflows SET trigger_type='schedule', trigger_config=%s, employee_id=%s, "
                "prompt_template=%s, action_type=%s, action_config=%s, risk_tier=%s, is_active=1 "
                "WHERE id=%s",
                (
                    f'{{"cron":"{cron}"}}',
                    patrol["employee_id"],
                    patrol["prompt"],
                    patrol["action_type"],
                    patrol["action_config"],
                    patrol["risk_tier"],
                    wf_id,
                ),
            )
        else:
            wf_id = await execute(
                "INSERT INTO workflows "
                "(name, description, trigger_type, trigger_config, employee_id, prompt_template, action_type, action_config, risk_tier, is_active) "
                "VALUES (%s,%s,'schedule',%s,%s,%s,%s,%s,%s,1)",
                (
                    patrol["name"],
                    "Automatically generated autonomous observability patrol.",
                    f'{{"cron":"{cron}"}}',
                    patrol["employee_id"],
                    patrol["prompt"],
                    patrol["action_type"],
                    patrol["action_config"],
                    patrol["risk_tier"],
                ),
            )
        results.append({"id": wf_id, "name": patrol["name"], "employee_id": patrol["employee_id"]})

    await reload_scheduled_workflows()

    for patrol in results:
        try:
            await trigger_workflow_manually(int(patrol["id"]), requested_by or "system")
        except Exception:
            pass

    return results


@router.get("/config")
async def get_observability_config(session: dict = Depends(get_session)):
    cfg = await _load_cfg()
    grafana_cfg = target_settings_for_ui(cfg, "grafana")
    grafana_cfg["payment_dashboard_url"] = str(cfg.get("grafana_payment_dashboard_url") or "")
    return {
        "grafana": grafana_cfg,
        "zabbix": target_settings_for_ui(cfg, "zabbix"),
        "kuma": target_settings_for_ui(cfg, "kuma"),
        "auto_monitor_enabled": bool(cfg.get("observability_auto_monitor_enabled")),
        "monitor_interval_minutes": int(cfg.get("observability_monitor_interval_minutes") or 5),
    }


@router.put("/config")
async def save_observability_config(
    body: ObservabilityConfigBody,
    session: dict = Depends(require_admin),
):
    updates: list[str] = []
    params: list[object] = []

    for field in ("grafana_url", "grafana_payment_dashboard_url", "zabbix_web_url", "kuma_app_url", "kuma_sync_url"):
        value = getattr(body, field)
        if value is not None:
            updates.append(f"{field}=%s")
            params.append(_normalize_url(value))

    kuma_public_value = body.kuma_public_url if body.kuma_public_url is not None else body.kuma_url
    if kuma_public_value is not None:
        normalized_public = _normalize_url(kuma_public_value)
        updates.extend(["kuma_public_url=%s", "kuma_url=%s"])
        params.extend([normalized_public, normalized_public])

    for field in ("grafana_username", "grafana_password", "zabbix_web_username", "zabbix_web_password", "kuma_username", "kuma_password"):
        value = getattr(body, field)
        if value is not None:
            updates.append(f"{field}=%s")
            params.append(str(value).strip())

    if body.auto_monitor_enabled is not None:
        updates.append("observability_auto_monitor_enabled=%s")
        params.append(1 if body.auto_monitor_enabled else 0)

    if body.monitor_interval_minutes is not None:
        updates.append("observability_monitor_interval_minutes=%s")
        params.append(int(body.monitor_interval_minutes))

    if updates:
        await execute("UPDATE zabbix_config SET " + ", ".join(updates), tuple(params))

    cfg = await _load_cfg()
    started = []
    if bool(cfg.get("observability_auto_monitor_enabled")):
        started = await _upsert_autonomous_patrols(cfg, session.get("username", "admin"))

    return {
        "ok": True,
        "auto_monitor_enabled": bool(cfg.get("observability_auto_monitor_enabled")),
        "monitor_interval_minutes": int(cfg.get("observability_monitor_interval_minutes") or 5),
        "started_workflows": started,
    }


@router.post("/start-monitoring")
async def start_observability_monitoring(session: dict = Depends(require_operator)):
    cfg = await _load_cfg()
    results = await _upsert_autonomous_patrols(cfg, session.get("username", "operator"))
    return {"ok": True, "workflows": results}


@router.get("/snapshot")
async def get_observability_snapshot(session: dict = Depends(get_session)):
    cfg = await _load_cfg()
    snapshot = await collect_monitoring_snapshot(cfg)
    return {
        "ok": True,
        "snapshot": snapshot,
        "summary": summarize_monitoring_snapshot(snapshot),
    }


@router.get("/overview")
async def get_observability_overview(session: dict = Depends(get_session)):
    cfg = await _load_cfg()
    data = await build_monitoring_overview(cfg)
    data["kuma_sync"] = await sync_kuma_override(cfg, data)
    return {"ok": True, **data}


@router.post("/sync-kuma")
async def sync_kuma_now(session: dict = Depends(require_operator)):
    cfg = await _load_cfg()
    data = await build_monitoring_overview(cfg)
    result = await sync_kuma_override(cfg, data)
    return {"ok": result.get("ok", False), "overview": data, "kuma_sync": result}


@router.get("/preflight")
async def preflight_dashboard(
    request: Request,
    url: str | None = Query(None, min_length=8, max_length=2000),
    target: str | None = Query(None),
    session: dict = Depends(get_session),
):
    cfg = await _load_cfg()
    requested_target = (target or "").strip().lower()
    if requested_target in {"grafana", "zabbix", "kuma"} and not url:
        url = target_settings(cfg, requested_target).get("url") or ""
    if not url:
        raise HTTPException(400, "A dashboard URL is required")

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(400, "Only http/https dashboard URLs are allowed")
    if parsed.username or parsed.password:
        raise HTTPException(400, "URLs with embedded credentials are not allowed")
    if not _allowed_target(parsed.hostname or "", request.url.hostname or "", cfg):
        raise HTTPException(400, "This dashboard host is not allowed for in-app embedding checks")

    matched_target, auth = credentials_for_url(cfg, url)

    try:
        result = await preflight_dashboard_url(
            url,
            str(request.base_url).rstrip("/"),
            auth=auth,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Preflight failed: {exc}")

    result["auth_used"] = bool(auth)
    result["matched_target"] = matched_target or requested_target or None
    return result


@router.get("/frame-proxy")
async def frame_proxy(
    request: Request,
    url: str = Query(..., min_length=8, max_length=2000),
    session: dict = Depends(get_session),
):
    """Server-side proxy for dashboard iframes — strips X-Frame-Options / CSP headers
    so Grafana/Zabbix/Kuma can be embedded even when they send deny/sameorigin.
    Auth credentials from the DB config are injected automatically.
    Only allowed dashboard hosts (configured in Observability settings) are proxied.
    """
    cfg = await _load_cfg()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(400, "Only http/https URLs allowed")
    if parsed.username or parsed.password:
        raise HTTPException(400, "URLs with embedded credentials are not allowed")
    if not _allowed_target(parsed.hostname or "", request.url.hostname or "", cfg):
        raise HTTPException(403, "Host not in configured dashboard list")

    _matched_target, auth = credentials_for_url(cfg, url)

    # Forward headers except host-specific ones
    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in {"host", "origin", "referer", "cookie", "authorization"}
    }

    _DROP_RESPONSE_HEADERS = {
        "x-frame-options", "content-security-policy",
        "content-security-policy-report-only", "transfer-encoding",
    }

    try:
        async with httpx.AsyncClient(verify=False, timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=forward_headers, auth=auth)

            # Stream response body, stripping embed-blocking headers
            safe_headers = {
                k: v for k, v in resp.headers.items()
                if k.lower() not in _DROP_RESPONSE_HEADERS
            }
            safe_headers["x-proxied-by"] = "noc-sentinel"
            content_type = resp.headers.get("content-type", "text/html")

            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=safe_headers,
                media_type=content_type,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Proxy error: {exc}")
