"""Shared observability target config and lightweight dashboard health checks."""

from __future__ import annotations

import re
import time
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.services.zabbix_client import call_zabbix


DEFAULT_TARGET_URLS: dict[str, str] = {
    "grafana": "https://grafana.tabadul.iq/dashboards",
    "zabbix": "https://zabbix.tabadul.iq/zabbix.php?action=dashboard.view&dashboardid=1&from=now-15m&to=now",
    "kuma": "",
}


TARGET_FIELD_MAP: dict[str, dict[str, str]] = {
    "grafana": {
        "url": "grafana_url",
        "username": "grafana_username",
        "password": "grafana_password",
    },
    "zabbix": {
        "url": "zabbix_web_url",
        "username": "zabbix_web_username",
        "password": "zabbix_web_password",
    },
    "kuma": {
        "url": "kuma_url",
    },
}


def normalize_target(target: str | None) -> str:
    value = str(target or "").strip().lower()
    return value if value in TARGET_FIELD_MAP else "grafana"


def mask_secret(value: str | None, *, keep_start: int = 2, keep_end: int = 2) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    if len(raw) <= keep_start + keep_end:
        return "*" * len(raw)
    return f"{raw[:keep_start]}{'*' * max(4, len(raw) - keep_start - keep_end)}{raw[-keep_end:]}"


def target_settings(cfg: dict | None, target: str | None) -> dict:
    config = cfg or {}
    normalized = normalize_target(target)
    fields = TARGET_FIELD_MAP[normalized]
    url_value = str(config.get(fields["url"]) or DEFAULT_TARGET_URLS.get(normalized, "")).strip()
    data = {
        "target": normalized,
        "url": url_value,
        "username": str(config.get(fields.get("username", "")) or "").strip(),
        "password": str(config.get(fields.get("password", "")) or "").strip(),
    }
    return data


def target_settings_for_ui(cfg: dict | None, target: str | None) -> dict:
    data = target_settings(cfg, target)
    return {
        "target": data["target"],
        "url": data["url"],
        "username": data["username"],
        "has_password": bool(data["password"]),
        "masked_password": mask_secret(data["password"]),
    }


def configured_dashboard_hosts(cfg: dict | None) -> set[str]:
    config = cfg or {}
    hosts: set[str] = set()
    for target in TARGET_FIELD_MAP:
        url_value = str(config.get(TARGET_FIELD_MAP[target]["url"]) or "").strip()
        if not url_value:
            continue
        parsed = urlparse(url_value)
        hostname = (parsed.hostname or "").strip().lower()
        if hostname:
            hosts.add(hostname)
    return hosts


def credentials_for_url(cfg: dict | None, raw_url: str) -> tuple[str | None, tuple[str, str] | None]:
    parsed = urlparse(raw_url)
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        return None, None
    for target in ("grafana", "zabbix"):
        data = target_settings(cfg, target)
        candidate = urlparse(data["url"])
        if (candidate.hostname or "").strip().lower() != hostname:
            continue
        username = data["username"]
        password = data["password"]
        if username and password:
            return target, (username, password)
        return target, None
    return None, None


def origin_from_url(raw_url: str) -> str:
    parsed = urlparse(str(raw_url or "").strip())
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def derive_kuma_sync_url(cfg: dict | None) -> str:
    target_cfg = target_settings(cfg, "kuma")
    origin = origin_from_url(target_cfg.get("url", ""))
    if not origin:
        return ""
    return f"{origin}/api/sentinel/override"


def extract_html_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return title


def extract_json_bool(raw: str, key: str) -> bool | None:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*(true|false)', raw or "", re.IGNORECASE)
    if not match:
        return None
    return match.group(1).lower() == "true"


def normalize_kuma_state(value: str | None) -> str:
    raw = str(value or "").strip().lower().replace(" ", "_")
    if raw in {"all_systems_operational", "operational", "up", "healthy"}:
        return "operational"
    if raw in {"degraded", "degraded_performance", "partial", "partial_outage"}:
        return "degraded"
    if raw in {"outage", "major_outage", "down"}:
        return "outage"
    return raw or "unknown"


def humanize_kuma_state(value: str | None) -> str:
    state = normalize_kuma_state(value)
    return {
        "operational": "All Systems Operational",
        "degraded": "Degraded Performance",
        "outage": "Major Service Outage",
        "unknown": "Unknown",
    }.get(state, state.replace("_", " ").title())


KUMA_STATUS_PHRASES: tuple[str, ...] = (
    "All Systems Operational",
    "Operational",
    "Degraded Performance",
    "Partial System Outage",
    "Major Service Outage",
    "Under Maintenance",
)


SERVICE_CATALOG: tuple[dict[str, object], ...] = (
    {
        "id": "payment_core",
        "label": "Card API",
        "owner": "Applications",
        "customer_facing": True,
        "impact": "Payment transactions may fail, stall, or return application errors.",
        "default_action": "Validate transaction flow, gateway health, and upstream issuer reachability.",
        "keywords": (
            "card api",
            "payment api",
            "issuer gateway",
            "visa gateway",
            "mastercard gateway",
            "payment switch",
            "transaction",
            "card switch",
        ),
        "grafana_keywords": ("api", "payment", "transaction", "gateway", "latency"),
        "kuma_keywords": ("card api", "payment api", "gateway", "transaction", "api"),
    },
    {
        "id": "pos_network",
        "label": "POS Network",
        "owner": "Merchant Operations",
        "customer_facing": True,
        "impact": "Merchants may lose terminal connectivity or authorization speed.",
        "default_action": "Check terminal reachability, site links, and merchant-facing error volume.",
        "keywords": ("pos", "terminal", "merchant", "branch pos", "atm"),
        "grafana_keywords": ("pos", "terminal", "merchant"),
        "kuma_keywords": ("pos", "terminal", "merchant"),
    },
    {
        "id": "external_connectivity",
        "label": "External Connectivity",
        "owner": "NOC",
        "customer_facing": True,
        "impact": "Upstream ISP or partner instability can degrade all external transaction paths.",
        "default_action": "Check ISP/BGP state, upstream latency, and packet loss before escalating carriers.",
        "keywords": (
            "isp",
            "bgp",
            "internet",
            "wan",
            "upstream",
            "dns",
            "packet loss",
            "latency",
        ),
        "grafana_keywords": ("wan", "internet", "latency", "packet loss", "external"),
        "kuma_keywords": ("connectivity", "internet", "wan", "upstream", "external"),
    },
    {
        "id": "core_network",
        "label": "Core Network",
        "owner": "Network Team",
        "customer_facing": True,
        "impact": "Core switching or routing issues can cascade across payment and branch services.",
        "default_action": "Inspect core switches, interfaces, SNMP reachability, and recent link-state changes.",
        "keywords": (
            "switch",
            "router",
            "firewall",
            "snmp",
            "icmp",
            "interface",
            "uplink",
            "link down",
            "network",
        ),
        "grafana_keywords": ("network", "switch", "router", "interface", "uplink"),
        "kuma_keywords": ("network", "switch", "router", "core"),
    },
    {
        "id": "platform_compute",
        "label": "Platform Compute",
        "owner": "Infrastructure",
        "customer_facing": False,
        "impact": "Server, VM, storage, or database issues can become customer-facing if sustained.",
        "default_action": "Validate host CPU, memory, disk, and database health before broader remediation.",
        "keywords": (
            "server",
            "vm",
            "cpu",
            "memory",
            "disk",
            "database",
            "mysql",
            "storage",
            "proliant",
            "linux",
            "windows",
        ),
        "grafana_keywords": ("inventory", "system", "trend", "host", "server", "database"),
        "kuma_keywords": ("platform", "compute", "database", "server"),
    },
    {
        "id": "observability_stack",
        "label": "Observability Stack",
        "owner": "SRE",
        "customer_facing": False,
        "impact": "Blind spots in monitoring or status publication reduce operator confidence and speed.",
        "default_action": "Restore telemetry collection, dashboard access, and status-page synchronization.",
        "keywords": ("grafana", "zabbix", "kuma", "monitor", "observability"),
        "grafana_keywords": ("grafana", "dashboard", "observability"),
        "kuma_keywords": ("kuma", "status page", "status"),
    },
)

SERVICE_STATUS_RANK: dict[str, int] = {
    "healthy": 0,
    "watch": 1,
    "degraded": 2,
    "critical": 3,
}

PUBLIC_STATE_RANK: dict[str, int] = {
    "operational": 0,
    "degraded": 1,
    "outage": 2,
    "unknown": -1,
}


def extract_kuma_status_text(html: str) -> str:
    text = str(html or "")
    text_lower = text.lower()
    for phrase in KUMA_STATUS_PHRASES:
        if phrase.lower() in text_lower:
            return phrase
    return ""


def _status_rank(state: str) -> int:
    value = str(state or "").strip().lower()
    mapping = {
        "operational": 0,
        "up": 0,
        "ok": 0,
        "healthy": 0,
        "watch": 1,
        "degraded": 1,
        "degraded_performance": 1,
        "partial": 1,
        "partial_outage": 1,
        "outage": 2,
        "down": 2,
        "major_outage": 2,
        "critical": 2,
    }
    return mapping.get(value, 0)


def _service_status_rank(state: str) -> int:
    return SERVICE_STATUS_RANK.get(str(state or "").strip().lower(), 0)


def _public_state_rank(state: str) -> int:
    return PUBLIC_STATE_RANK.get(normalize_kuma_state(state), -1)


def _public_state_to_recommendation(state: str) -> str:
    normalized = normalize_kuma_state(state)
    if normalized == "outage":
        return "major_outage"
    if normalized == "degraded":
        return "degraded_performance"
    return "up"


def _append_unique(items: list[str], value: str | None, *, limit: int = 4) -> None:
    text = str(value or "").strip()
    if not text or text in items:
        return
    items.append(text)
    if len(items) > limit:
        del items[limit:]


def _match_service_from_text(
    text: str | None,
    keyword_field: str = "keywords",
    *,
    default: str = "",
) -> str:
    haystack = str(text or "").strip().lower()
    if not haystack:
        return default

    best_service = default
    best_score = 0
    for service in SERVICE_CATALOG:
        score = 0
        for keyword in service.get(keyword_field, ()) or ():
            token = str(keyword or "").strip().lower()
            if token and token in haystack:
                score += max(2, len(token.split()))
        if score > best_score:
            best_score = score
            best_service = str(service.get("id") or default)

    if best_score:
        return best_service

    if any(marker in haystack for marker in ("cpu", "memory", "disk", "server", "vm", "database", "host")):
        return "platform_compute"
    if any(marker in haystack for marker in ("switch", "router", "snmp", "icmp", "link", "uplink")):
        return "core_network"
    if any(marker in haystack for marker in ("transaction", "gateway", "payment", "issuer")):
        return "payment_core"
    return default


def _service_status_to_public_state(service: dict) -> str:
    if not bool(service.get("customer_facing")):
        return "operational"
    state = str(service.get("status") or "healthy")
    if state == "critical":
        return "outage"
    if state in {"degraded", "watch"}:
        return "degraded"
    return "operational"


def _promote_service_status(service: dict, new_state: str) -> None:
    current = str(service.get("status") or "healthy")
    if _service_status_rank(new_state) > _service_status_rank(current):
        service["status"] = new_state


def _blank_service_board() -> dict[str, dict]:
    board: dict[str, dict] = {}
    for service in SERVICE_CATALOG:
        service_id = str(service["id"])
        board[service_id] = {
            "id": service_id,
            "label": str(service["label"]),
            "owner": str(service["owner"]),
            "customer_facing": bool(service["customer_facing"]),
            "impact": str(service["impact"]),
            "default_action": str(service["default_action"]),
            "status": "healthy",
            "issue_count": 0,
            "critical_signals": 0,
            "expected_public_state": "operational",
            "current_public_state": "unknown",
            "kuma_alignment": "unknown",
            "source_states": {
                "zabbix": "clear",
                "grafana": "unknown",
                "kuma": "unknown",
            },
            "matched_dashboards": [],
            "matched_groups": [],
            "evidence": [],
            "next_actions": [],
        }
    return board


def build_service_monitoring_board(zabbix: dict, grafana: dict, kuma: dict) -> list[dict]:
    board = _blank_service_board()

    if str(zabbix.get("status") or "unknown") == "error":
        service = board["observability_stack"]
        _promote_service_status(service, "degraded")
        service["source_states"]["zabbix"] = "unavailable"
        _append_unique(service["evidence"], "Zabbix API is unavailable, so service correlation fidelity is reduced.")
        _append_unique(service["next_actions"], service["default_action"])

    for problem in (zabbix.get("top_problems") or [])[:8]:
        if not isinstance(problem, dict):
            continue
        host = str(problem.get("host") or "unknown-host")
        name = str(problem.get("name") or "Unnamed problem")
        severity = int(problem.get("severity") or 0)
        service_id = _match_service_from_text(f"{host} {name}", "keywords", default="platform_compute")
        service = board.get(service_id) or board["platform_compute"]
        service["issue_count"] = int(service.get("issue_count") or 0) + 1
        if severity >= 4:
            service["critical_signals"] = int(service.get("critical_signals") or 0) + 1
        service["source_states"]["zabbix"] = "active_alerts"
        _promote_service_status(
            service,
            "critical" if severity >= 4 else "degraded" if severity >= 2 else "watch",
        )
        _append_unique(service["evidence"], f"{host} :: {name}")
        _append_unique(service["next_actions"], service["default_action"])

    grafana_stack = board["observability_stack"]
    grafana_status = str(grafana.get("status") or "unknown")
    grafana_auth = str(grafana.get("auth_status") or "unknown")
    if grafana_status == "ok":
        grafana_stack["source_states"]["grafana"] = "telemetry_ready"
        _append_unique(grafana_stack["evidence"], "Grafana API is reachable and telemetry summaries are available.")
    elif grafana_status != "not_configured":
        grafana_stack["source_states"]["grafana"] = "degraded"
        _promote_service_status(grafana_stack, "critical" if grafana_status == "error" else "degraded")
        _append_unique(
            grafana_stack["evidence"],
            "Grafana API health is "
            + str(grafana.get("api_health") or grafana_status)
            + (f" ({grafana_auth.replace('_', ' ')})" if grafana_auth not in {"", "unknown", "not_configured"} else ""),
        )
        _append_unique(grafana_stack["next_actions"], grafana_stack["default_action"])

    for item in (grafana.get("dashboards") or [])[:8]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "Untitled")
        service_id = _match_service_from_text(title, "grafana_keywords")
        if not service_id:
            continue
        service = board.get(service_id)
        if not service:
            continue
        service["source_states"]["grafana"] = "telemetry_ready"
        _append_unique(service["matched_dashboards"], title, limit=3)
        _append_unique(service["evidence"], f"Grafana dashboard available: {title}", limit=5)

    kuma_status = str(kuma.get("status") or "unknown")
    kuma_groups = kuma.get("groups") or []
    if kuma_status == "error":
        grafana_stack["source_states"]["kuma"] = "unavailable"
        _promote_service_status(grafana_stack, "degraded")
        _append_unique(grafana_stack["evidence"], "Kuma public status API is unavailable.")
        _append_unique(grafana_stack["next_actions"], grafana_stack["default_action"])

    for group in kuma_groups[:10]:
        if not isinstance(group, dict):
            continue
        label = str(group.get("label") or "Public Group")
        service_id = _match_service_from_text(label, "kuma_keywords")
        if not service_id:
            continue
        service = board.get(service_id)
        if not service:
            continue
        group_state = normalize_kuma_state(group.get("status"))
        if _public_state_rank(group_state) > _public_state_rank(service.get("current_public_state")):
            service["current_public_state"] = group_state
        service["source_states"]["kuma"] = "published"
        _append_unique(service["matched_groups"], label, limit=3)
        if group_state == "outage":
            _promote_service_status(service, "degraded")
            _append_unique(service["evidence"], f"Kuma group '{label}' is published as Major Service Outage.")
        elif group_state == "degraded":
            _promote_service_status(service, "watch")
            _append_unique(service["evidence"], f"Kuma group '{label}' is published as Degraded Performance.")

    kuma_overall = normalize_kuma_state(kuma.get("page_state") or kuma.get("status_text"))
    for service in board.values():
        service["expected_public_state"] = _service_status_to_public_state(service)
        if service.get("current_public_state") == "unknown":
            if bool(service.get("customer_facing")) and service.get("expected_public_state") != "operational":
                service["kuma_alignment"] = "unmapped"
            elif kuma_overall != "unknown" and not bool(service.get("customer_facing")):
                service["kuma_alignment"] = "internal_only"
            else:
                service["kuma_alignment"] = "unknown"
        elif normalize_kuma_state(service.get("current_public_state")) == normalize_kuma_state(service.get("expected_public_state")):
            service["kuma_alignment"] = "aligned"
        else:
            service["kuma_alignment"] = "mismatch"

    services = list(board.values())
    services.sort(
        key=lambda item: (
            -_service_status_rank(str(item.get("status") or "healthy")),
            -int(item.get("critical_signals") or 0),
            -int(item.get("issue_count") or 0),
            0 if item.get("customer_facing") else 1,
            str(item.get("label") or ""),
        )
    )
    return services


def dashboard_access_state(result: dict | None) -> str:
    data = result or {}
    if data.get("requires_login"):
        return "login_required"
    if data.get("frame_allowed") is False:
        return "embed_blocked"
    if data.get("ok"):
        return "ok"
    return str(data.get("status") or "unknown")


def detect_login_required(body_snippet: str, final_url: str) -> bool:
    snippet_lower = (body_snippet or "").lower()
    return any(
        marker in snippet_lower
        for marker in (
            "you are not logged in",
            "must login to view this page",
            "<title>login",
            "name=\"login\"",
            "signin",
            "log in",
            "sign in",
        )
    ) or "/login" in (final_url or "").lower()


def frame_policy_status(headers: httpx.Headers, request_origin: str) -> tuple[bool | None, str]:
    xfo = (headers.get("x-frame-options") or "").strip()
    csp = (headers.get("content-security-policy") or "").strip()
    reasons: list[str] = []

    if xfo:
        lowered = xfo.lower()
        if "deny" in lowered:
            reasons.append(f"X-Frame-Options={xfo}")
        elif "sameorigin" in lowered:
            reasons.append(f"X-Frame-Options={xfo}")

    frame_ancestors = ""
    if csp:
        for directive in csp.split(";"):
            chunk = directive.strip()
            if chunk.lower().startswith("frame-ancestors"):
                frame_ancestors = chunk
                break
        if frame_ancestors:
            lowered = frame_ancestors.lower()
            if "'none'" in lowered:
                reasons.append(frame_ancestors)
            elif "*" not in frame_ancestors and request_origin not in frame_ancestors:
                reasons.append(frame_ancestors)

    if reasons:
        return False, " | ".join(reasons)
    if xfo or frame_ancestors:
        return True, "Embedding headers present but not obviously blocking"
    return None, "No explicit frame policy headers detected"


async def preflight_dashboard_url(
    url: str,
    request_origin: str,
    *,
    auth: tuple[str, str] | None = None,
    max_chars: int = 4096,
) -> dict:
    async with httpx.AsyncClient(
        timeout=12,
        verify=settings.outbound_tls_verify,
        follow_redirects=True,
        auth=auth,
    ) as client:
        async with client.stream(
            "GET",
            url,
            headers={
                "User-Agent": "NOC-Sentinel-Observability-Preflight/1.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        ) as response:
            frame_allowed, frame_reason = frame_policy_status(
                response.headers,
                request_origin,
            )
            final_url = str(response.url)
            status_code = response.status_code
            x_frame_options = (response.headers.get("x-frame-options") or "").strip()
            content_security_policy = (response.headers.get("content-security-policy") or "").strip()
            snippet_parts: list[str] = []
            collected = 0
            async for chunk in response.aiter_text():
                snippet_parts.append(chunk)
                collected += len(chunk)
                if collected >= max_chars:
                    break
            body_snippet = "".join(snippet_parts)[:max_chars]

    snippet_lower = body_snippet.lower()
    client_frame_bust = any(
        marker in snippet_lower
        for marker in (
            "top.location",
            "window.top",
            "parent.location",
            "if (top",
            "if(top",
        )
    )

    return {
        "ok": True,
        "requested_url": url,
        "final_url": final_url,
        "status_code": status_code,
        "frame_allowed": frame_allowed,
        "frame_reason": frame_reason,
        "requires_login": detect_login_required(body_snippet, final_url),
        "client_frame_bust": client_frame_bust,
        "headers": {
            "x_frame_options": x_frame_options,
            "content_security_policy": content_security_policy,
        },
    }


async def collect_monitoring_snapshot(cfg: dict | None) -> dict:
    config = cfg or {}
    snapshot: dict = {
        "zabbix": {
            "problem_count": 0,
            "top_problems": [],
            "status": "unknown",
        },
        "dashboards": {},
    }

    try:
        problems = await call_zabbix(
            "problem.get",
            {
                "output": ["eventid", "name", "severity"],
                "selectHosts": ["host", "name"],
                "recent": True,
                "sortfield": "eventid",
                "sortorder": "DESC",
                "limit": 12,
            },
        )
        if isinstance(problems, list):
            top_problems = []
            for item in problems[:12]:
                hosts = item.get("hosts") if isinstance(item, dict) else []
                first_host = ""
                if isinstance(hosts, list) and hosts:
                    host_row = hosts[0] if isinstance(hosts[0], dict) else {}
                    first_host = str(host_row.get("host") or host_row.get("name") or "").strip()
                top_problems.append(
                    {
                        "name": item.get("name", "Unknown problem"),
                        "severity": int(item.get("severity") or 0),
                        "host": first_host,
                    }
                )
            snapshot["zabbix"] = {
                "problem_count": len(problems),
                "top_problems": top_problems,
                "status": "ok",
            }
        elif isinstance(problems, dict) and problems.get("_zabbix_error"):
            snapshot["zabbix"] = {
                "problem_count": 0,
                "top_problems": [],
                "status": "error",
                "error": problems.get("_zabbix_error"),
            }
    except Exception as exc:
        snapshot["zabbix"] = {
            "problem_count": 0,
            "top_problems": [],
            "status": "error",
            "error": str(exc),
        }

    for target in ("grafana", "zabbix", "kuma"):
        target_cfg = target_settings(config, target)
        url = target_cfg.get("url", "")
        if not url:
            snapshot["dashboards"][target] = {"status": "not_configured"}
            continue
        auth = None
        if target_cfg.get("username") and target_cfg.get("password"):
            auth = (target_cfg["username"], target_cfg["password"])
        try:
            result = await preflight_dashboard_url(url, "http://localhost", auth=auth, max_chars=2048)
            status = "ok"
            if result.get("requires_login"):
                status = "login_required"
            elif result.get("frame_allowed") is False:
                status = "embed_blocked"
            snapshot["dashboards"][target] = {
                "status": status,
                "auth_used": bool(auth),
                "frame_allowed": result.get("frame_allowed"),
                "requires_login": result.get("requires_login"),
                "frame_reason": result.get("frame_reason"),
                "final_url": result.get("final_url"),
            }
        except Exception as exc:
            snapshot["dashboards"][target] = {
                "status": "error",
                "error": str(exc),
                "auth_used": bool(auth),
            }

    return snapshot


async def collect_zabbix_overview(cfg: dict | None, snapshot: dict | None = None) -> dict:
    snap = snapshot or {}
    zabbix_data = snap.get("zabbix") or {}
    overview = {
        "status": zabbix_data.get("status", "unknown"),
        "host_count": 0,
        "problem_count": int(zabbix_data.get("problem_count") or 0),
        "critical_problem_count": 0,
        "top_problems": zabbix_data.get("top_problems") or [],
    }
    try:
        host_count = await call_zabbix("host.get", {"countOutput": True})
        overview["host_count"] = int(host_count or 0)
    except Exception as exc:
        overview["host_count_error"] = str(exc)

    top_problems = overview["top_problems"]
    if isinstance(top_problems, list):
        overview["critical_problem_count"] = sum(
            1 for item in top_problems if int(item.get("severity") or 0) >= 4
        )
    return overview


async def collect_grafana_overview(cfg: dict | None, snapshot: dict | None = None) -> dict:
    config = cfg or {}
    target_cfg = target_settings(config, "grafana")
    url = target_cfg.get("url", "")
    auth = None
    if target_cfg.get("username") and target_cfg.get("password"):
        auth = (target_cfg["username"], target_cfg["password"])

    overview = {
        "status": "not_configured" if not url else "unknown",
        "url": url,
        "version": "",
        "commit": "",
        "api_health": "unknown",
        "auth_status": "not_configured" if not auth else "unknown",
        "dashboard_access": dashboard_access_state((snapshot or {}).get("dashboards", {}).get("grafana")),
        "auth_hint": "",
        "dashboards": [],
    }
    if not url:
        return overview

    origin = origin_from_url(url)
    if not origin:
        overview["status"] = "error"
        overview["error"] = "Invalid Grafana URL"
        return overview

    try:
        async with httpx.AsyncClient(
            timeout=12,
            verify=settings.outbound_tls_verify,
            follow_redirects=True,
        ) as client:
            health = await client.get(f"{origin}/api/health", auth=auth or None)
            if health.status_code == 200:
                overview["api_health"] = "ok"
                overview["status"] = "ok"
                try:
                    payload = health.json()
                    overview["version"] = str(payload.get("version") or "")
                    overview["commit"] = str(payload.get("commit") or "")
                except Exception:
                    pass
            elif health.status_code in (401, 403):
                overview["api_health"] = "auth_required"
                overview["status"] = "degraded"
            else:
                overview["api_health"] = f"http_{health.status_code}"
                overview["status"] = "degraded"

            if auth:
                search = await client.get(f"{origin}/api/search?limit=6", auth=auth)
                if search.status_code == 200:
                    overview["auth_status"] = "ok"
                    try:
                        items = search.json()
                        if isinstance(items, list):
                            dashboard_items = [
                                item for item in items
                                if str(item.get("type") or "dash-db") in {"dash-db", "dash-folder"}
                            ]
                            if not dashboard_items:
                                dashboard_items = items
                            overview["dashboard_count"] = len(dashboard_items)
                            overview["dashboards"] = [
                                {
                                    "title": str(item.get("title") or item.get("name") or "Untitled"),
                                    "url": str(item.get("url") or ""),
                                    "type": str(item.get("type") or ""),
                                }
                                for item in dashboard_items[:6]
                            ]
                        else:
                            overview["dashboard_count"] = 0
                    except Exception:
                        overview["dashboard_count"] = 0
                elif search.status_code in (401, 403):
                    login_page = await client.get(
                        f"{origin}/login",
                        headers={
                            "User-Agent": "NOC-Sentinel-Observability-Preflight/1.0",
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        },
                    )
                    login_html = login_page.text[:50000] if login_page.status_code == 200 else ""
                    login_form_enabled = extract_json_bool(login_html, "disableLoginForm")
                    ldap_enabled = extract_json_bool(login_html, "ldapEnabled")
                    if login_form_enabled is False:
                        overview["auth_status"] = "interactive_login_required"
                        hint = "Grafana rejected API authentication but still exposes the interactive browser login form."
                        if ldap_enabled:
                            hint += " This instance appears to use LDAP-backed sign-in, so browser login can work even when API Basic auth does not."
                        overview["auth_hint"] = hint
                    else:
                        overview["auth_status"] = "invalid_credentials"
                    if overview["status"] == "ok":
                        overview["status"] = "degraded"
                else:
                    overview["auth_status"] = f"http_{search.status_code}"
                    if overview["status"] == "ok":
                        overview["status"] = "degraded"
    except Exception as exc:
        overview["status"] = "error"
        overview["error"] = str(exc)

    return overview


async def collect_kuma_overview(cfg: dict | None, summary: dict | None = None) -> dict:
    config = cfg or {}
    target_cfg = target_settings(config, "kuma")
    url = target_cfg.get("url", "")
    recommendation = summary or {}
    overview = {
        "status": "not_configured" if not url else "unknown",
        "url": url,
        "page_title": "",
        "status_text": "",
        "page_state": "unknown",
        "group_count": 0,
        "problem_count": 0,
        "groups": [],
        "sentinel_override_active": False,
        "recommended_state": str(recommendation.get("recommended_kuma_state") or "up"),
        "recommended_note": str(recommendation.get("recommended_kuma_note") or ""),
    }
    if not url:
        return overview

    try:
        origin = origin_from_url(url)
        async with httpx.AsyncClient(
            timeout=12,
            verify=settings.outbound_tls_verify,
            follow_redirects=True,
        ) as client:
            api_response = None
            if origin:
                try:
                    api_response = await client.get(
                        f"{origin}/api/status",
                        headers={
                            "User-Agent": "NOC-Sentinel-Kuma-Overview/1.0",
                            "Accept": "application/json,text/plain,*/*",
                        },
                    )
                except Exception:
                    api_response = None

            if api_response is not None and api_response.status_code == 200:
                payload = api_response.json()
                groups = payload.get("groups") if isinstance(payload, dict) else []
                problems = payload.get("problems") if isinstance(payload, dict) else []
                overall = normalize_kuma_state((payload or {}).get("overall"))
                overview["http_status"] = api_response.status_code
                overview["page_state"] = overall
                overview["status_text"] = humanize_kuma_state(overall)
                overview["status"] = "ok"
                overview["api_status"] = "ok"
                overview["group_count"] = len(groups) if isinstance(groups, list) else 0
                overview["problem_count"] = len(problems) if isinstance(problems, list) else 0
                overview["groups"] = [
                    {
                        "label": str(item.get("label") or item.get("id") or "Group"),
                        "status": normalize_kuma_state(item.get("status")),
                        "down_count": int(item.get("downCount") or 0),
                        "degraded_count": int(item.get("degradedCount") or 0),
                    }
                    for item in (groups[:6] if isinstance(groups, list) else [])
                ]
                override = (payload or {}).get("sentinelOverride") or {}
                overview["sentinel_override_active"] = bool(
                    isinstance(override, dict) and override.get("active")
                )
                return overview

            response = await client.get(
                url,
                headers={
                    "User-Agent": "NOC-Sentinel-Kuma-Overview/1.0",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
        html = response.text[:50000]
        overview["http_status"] = response.status_code
        overview["page_title"] = extract_html_title(html)
        overview["status_text"] = extract_kuma_status_text(html)
        overview["page_state"] = normalize_kuma_state(overview["status_text"])
        overview["status"] = "ok" if response.status_code == 200 else f"http_{response.status_code}"
    except Exception as exc:
        overview["status"] = "error"
        overview["error"] = str(exc)

    return overview


async def build_monitoring_overview(cfg: dict | None) -> dict:
    snapshot = await collect_monitoring_snapshot(cfg)
    zabbix = await collect_zabbix_overview(cfg, snapshot)
    grafana = await collect_grafana_overview(cfg, snapshot)
    seed_summary = summarize_monitoring_snapshot(snapshot)
    kuma = await collect_kuma_overview(cfg, seed_summary)
    services = build_service_monitoring_board(zabbix, grafana, kuma)
    summary = summarize_monitoring_sources(snapshot, zabbix, grafana, kuma, services)
    kuma["recommended_state"] = str(summary.get("recommended_kuma_state") or kuma.get("recommended_state") or "up")
    kuma["recommended_note"] = str(summary.get("recommended_kuma_note") or kuma.get("recommended_note") or "")
    episodes = [
        {
            "service_id": item.get("id"),
            "label": item.get("label"),
            "status": item.get("status"),
            "owner": item.get("owner"),
            "issue_count": int(item.get("issue_count") or 0),
            "customer_facing": bool(item.get("customer_facing")),
            "expected_public_state": item.get("expected_public_state"),
            "current_public_state": item.get("current_public_state"),
            "summary": (item.get("evidence") or ["No evidence collected yet."])[0],
            "next_action": (item.get("next_actions") or [item.get("default_action") or "Review the live signals."])[0],
        }
        for item in services
        if str(item.get("status") or "healthy") != "healthy"
    ]
    return {
        "snapshot": snapshot,
        "summary": summary,
        "services": services,
        "episodes": episodes,
        "sources": {
            "zabbix": zabbix,
            "grafana": grafana,
            "kuma": kuma,
        },
    }

def _kuma_status_to_page_status(state: str) -> str:
    value = str(state or "").strip().lower()
    if value in {"major_outage", "outage", "down", "critical"}:
        return "outage"
    if value in {"degraded_performance", "degraded", "partial", "partial_outage"}:
        return "degraded"
    return "operational"


async def sync_kuma_override(cfg: dict | None, overview: dict | None) -> dict:
    sync_url = derive_kuma_sync_url(cfg)
    if not sync_url:
        return {"ok": False, "status": "not_configured", "detail": "Kuma URL is not configured"}

    data = overview or {}
    summary = data.get("summary") or {}
    zabbix = (data.get("sources") or {}).get("zabbix") or {}
    grafana = (data.get("sources") or {}).get("grafana") or {}
    kuma = (data.get("sources") or {}).get("kuma") or {}
    services = data.get("services") or []

    grafana_status = "operational"
    if grafana.get("status") in {"error"}:
        grafana_status = "outage"
    elif grafana.get("auth_status") == "invalid_credentials" or grafana.get("dashboard_access") in {"embed_blocked", "login_required"}:
        grafana_status = "degraded"

    service_groups: dict[str, dict] = {}
    for item in services:
        if not isinstance(item, dict) or not item.get("customer_facing"):
            continue
        service_groups[str(item.get("id") or item.get("label") or "service")] = {
            "label": str(item.get("label") or "Service"),
            "status": normalize_kuma_state(item.get("expected_public_state") or "operational"),
            "owner": str(item.get("owner") or ""),
            "issue_count": int(item.get("issue_count") or 0),
            "critical_signals": int(item.get("critical_signals") or 0),
            "kuma_alignment": str(item.get("kuma_alignment") or "unknown"),
            "impact": str(item.get("impact") or ""),
            "evidence": list(item.get("evidence") or [])[:2],
        }

    payload = {
        "source": "noc-sentinel",
        "derived_at": int(time.time()),
        "overall_status": _kuma_status_to_page_status(summary.get("recommended_kuma_state") or "up"),
        "headline": str(summary.get("headline") or ""),
        "recommended_kuma_state": str(summary.get("recommended_kuma_state") or "up"),
        "recommended_kuma_note": str(summary.get("recommended_kuma_note") or ""),
        "expires_in_seconds": 420,
        "groups": service_groups,
        "source_groups": {
            "zabbix": {
                "status": _kuma_status_to_page_status(summary.get("recommended_kuma_state") or "up"),
                "problem_count": int(zabbix.get("problem_count") or 0),
                "host_count": int(zabbix.get("host_count") or 0),
                "critical_problem_count": int(zabbix.get("critical_problem_count") or 0),
            },
            "grafana": {
                "status": grafana_status,
                "api_health": str(grafana.get("api_health") or "unknown"),
                "auth_status": str(grafana.get("auth_status") or "unknown"),
                "dashboard_access": str(grafana.get("dashboard_access") or "unknown"),
                "dashboard_count": int(grafana.get("dashboard_count") or 0),
            },
            "kuma": {
                "status": _kuma_status_to_page_status(kuma.get("status") or "operational"),
                "status_text": str(kuma.get("status_text") or ""),
            },
        },
        "service_count": len(service_groups),
        "abnormalities": list(summary.get("abnormalities") or []),
    }

    try:
        async with httpx.AsyncClient(
            timeout=10,
            verify=settings.outbound_tls_verify,
            follow_redirects=True,
        ) as client:
            response = await client.post(
                sync_url,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "NOC-Sentinel-KumaSync/1.0",
                },
                json=payload,
            )
        if response.status_code >= 400:
            return {
                "ok": False,
                "status": f"http_{response.status_code}",
                "detail": response.text[:240],
                "url": sync_url,
            }
        body: dict | None = None
        try:
            body = response.json()
        except Exception:
            body = None
        return {
            "ok": True,
            "status": "sent",
            "url": sync_url,
            "response": body or response.text[:200],
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "detail": str(exc),
            "url": sync_url,
        }


def summarize_monitoring_snapshot(snapshot: dict) -> dict:
    zabbix = snapshot.get("zabbix") or {}
    dashboards = snapshot.get("dashboards") or {}

    problem_count = int(zabbix.get("problem_count") or 0)
    zabbix_status = str(zabbix.get("status") or "unknown")
    abnormalities: list[str] = []
    overall_status = "healthy"

    if zabbix_status == "error":
        abnormalities.append("Zabbix API is unavailable.")
        overall_status = "critical"
    elif problem_count > 0:
        abnormalities.append(f"{problem_count} active Zabbix problem(s) detected.")
        overall_status = "critical" if problem_count >= 5 else "degraded"

    blocked_targets: list[str] = []
    login_targets: list[str] = []
    broken_targets: list[str] = []
    for target in ("grafana", "zabbix", "kuma"):
        status = str((dashboards.get(target) or {}).get("status") or "unknown")
        if status == "embed_blocked":
            blocked_targets.append(target)
        elif status == "login_required":
            login_targets.append(target)
        elif status == "error":
            broken_targets.append(target)

    if blocked_targets:
        abnormalities.append(
            "Embed blocked: " + ", ".join(name.title() for name in blocked_targets) + "."
        )
        if overall_status == "healthy":
            overall_status = "degraded"
    if login_targets:
        abnormalities.append(
            "Login required: " + ", ".join(name.title() for name in login_targets) + "."
        )
        if overall_status == "healthy":
            overall_status = "degraded"
    if broken_targets:
        abnormalities.append(
            "Dashboard checks failed: " + ", ".join(name.title() for name in broken_targets) + "."
        )
        overall_status = "critical"

    if abnormalities:
        headline = abnormalities[0]
    else:
        headline = "All configured observability checks look healthy."

    kuma_state = "up"
    if overall_status == "critical":
        kuma_state = "major_outage"
    elif overall_status == "degraded":
        kuma_state = "degraded_performance"

    if abnormalities:
        kuma_note = " ".join(abnormalities[:2])
    else:
        kuma_note = "No active NOC abnormalities detected from the current snapshot."

    return {
        "overall_status": overall_status,
        "headline": headline,
        "abnormalities": abnormalities,
        "recommended_kuma_state": kuma_state,
        "recommended_kuma_note": kuma_note,
        "problem_count": problem_count,
    }


def summarize_monitoring_sources(
    snapshot: dict,
    zabbix: dict,
    grafana: dict,
    kuma: dict,
    services: list[dict] | None = None,
) -> dict:
    problem_count = int(zabbix.get("problem_count") or 0)
    service_rows = list(services or [])
    affected_services = [item for item in service_rows if str(item.get("status") or "healthy") != "healthy"]
    customer_facing_issues = [
        item for item in service_rows
        if bool(item.get("customer_facing")) and normalize_kuma_state(item.get("expected_public_state")) != "operational"
    ]
    internal_issues = [
        item for item in affected_services
        if not bool(item.get("customer_facing"))
    ]
    abnormalities: list[str] = []
    actions: list[str] = []

    highest_service_rank = max(
        (_service_status_rank(str(item.get("status") or "healthy")) for item in service_rows),
        default=0,
    )
    if highest_service_rank >= 3:
        overall_status = "critical"
    elif highest_service_rank >= 1:
        overall_status = "degraded"
    else:
        overall_status = "healthy"

    for item in affected_services[:4]:
        evidence = (item.get("evidence") or ["No correlated evidence yet."])[0]
        abnormalities.append(
            f"{item.get('label')}: {evidence}"
        )
        next_action = (item.get("next_actions") or [item.get("default_action") or "Review live signals."])[0]
        actions.append(f"{item.get('label')}: {next_action}")

    if str(zabbix.get("status") or "unknown") == "error":
        abnormalities.append("Zabbix API is unavailable, so service correlation is partially degraded.")
        if overall_status != "critical":
            overall_status = "degraded"

    grafana_api_health = str(grafana.get("api_health") or "unknown")
    if str(grafana.get("status") or "unknown") == "error" or grafana_api_health not in {"ok", "unknown"}:
        abnormalities.append("Grafana telemetry is not fully available for correlation.")
        if overall_status == "healthy":
            overall_status = "degraded"
    elif int(grafana.get("dashboard_count") or 0) > 0:
        titles = [
            str(item.get("title") or "")
            for item in (grafana.get("dashboards") or [])[:3]
            if isinstance(item, dict)
        ]
        if titles:
            actions.append("Grafana dashboards ready: " + ", ".join(titles))

    kuma_public_state = normalize_kuma_state(kuma.get("page_state") or kuma.get("status_text"))
    if str(kuma.get("status") or "unknown") == "error":
        abnormalities.append("Kuma public status API is unavailable.")
        if overall_status == "healthy":
            overall_status = "degraded"

    mismatch_services = [
        item for item in service_rows
        if bool(item.get("customer_facing")) and str(item.get("kuma_alignment") or "") == "mismatch"
    ]
    unmapped_services = [
        item for item in service_rows
        if bool(item.get("customer_facing")) and str(item.get("kuma_alignment") or "") == "unmapped"
    ]

    if mismatch_services:
        mismatched = mismatch_services[0]
        abnormalities.append(
            "Kuma still shows "
            + str(mismatched.get("label") or "a customer-facing service")
            + " as "
            + humanize_kuma_state(mismatched.get("current_public_state") or "unknown")
            + " while NOC expects "
            + humanize_kuma_state(mismatched.get("expected_public_state") or "unknown")
            + "."
        )
        if overall_status == "healthy":
            overall_status = "degraded"
    elif unmapped_services:
        unmapped = unmapped_services[0]
        abnormalities.append(
            "Kuma does not expose a mapped public group for "
            + str(unmapped.get("label") or "a customer-facing service")
            + "."
        )
        if overall_status == "healthy":
            overall_status = "degraded"

    expected_public_state = "operational"
    for item in customer_facing_issues:
        candidate = normalize_kuma_state(item.get("expected_public_state") or "operational")
        if _public_state_rank(candidate) > _public_state_rank(expected_public_state):
            expected_public_state = candidate

    recommended_kuma_state = _public_state_to_recommendation(expected_public_state)
    if customer_facing_issues:
        labels = ", ".join(str(item.get("label") or "Service") for item in customer_facing_issues[:3])
        recommended_kuma_note = (
            "Customer-facing services need a public update: "
            + labels
            + "."
        )
    elif internal_issues:
        labels = ", ".join(str(item.get("label") or "Service") for item in internal_issues[:3])
        recommended_kuma_note = (
            "Internal services are degraded ("
            + labels
            + "), but public status can remain operational unless customer impact is confirmed."
        )
    else:
        recommended_kuma_note = "Core services are healthy and public status can remain operational."

    if customer_facing_issues:
        top = customer_facing_issues[0]
        headline = (
            str(top.get("label") or "Customer-facing service")
            + " is "
            + str(top.get("status") or "degraded")
            + " and needs immediate attention."
        )
    elif affected_services:
        top = affected_services[0]
        headline = (
            str(top.get("label") or "Service")
            + " needs attention before it becomes customer-facing."
        )
    else:
        headline = "Core services are healthy and the monitoring stack is aligned."

    if int(kuma.get("problem_count") or 0) > 0:
        actions.append(f"Kuma public page already shows {int(kuma.get('problem_count') or 0)} public issue(s).")

    global_kuma_alignment = "aligned"
    if mismatch_services:
        global_kuma_alignment = "mismatch"
    elif unmapped_services:
        global_kuma_alignment = "unmapped"
    elif (
        expected_public_state == "operational"
        and kuma_public_state not in {"unknown", "operational"}
    ):
        global_kuma_alignment = "mismatch"

    return {
        "overall_status": overall_status,
        "headline": headline,
        "abnormalities": abnormalities,
        "actions": actions,
        "recommended_kuma_state": recommended_kuma_state,
        "recommended_kuma_note": recommended_kuma_note,
        "problem_count": problem_count,
        "kuma_public_state": kuma_public_state,
        "kuma_alignment": global_kuma_alignment,
        "affected_services": [
            {
                "id": item.get("id"),
                "label": item.get("label"),
                "status": item.get("status"),
                "expected_public_state": item.get("expected_public_state"),
                "current_public_state": item.get("current_public_state"),
            }
            for item in affected_services[:5]
        ],
        "customer_facing_count": len(customer_facing_issues),
    }


def snapshot_prompt_context(snapshot: dict) -> str:
    zabbix = snapshot.get("zabbix") or {}
    dashboards = snapshot.get("dashboards") or {}

    lines = [
        "LIVE OBSERVABILITY SNAPSHOT:",
        f"- Zabbix API status: {zabbix.get('status', 'unknown')}",
        f"- Active problems: {zabbix.get('problem_count', 0)}",
    ]
    for problem in (zabbix.get("top_problems") or [])[:5]:
        lines.append(
            f"  * [{problem.get('severity', 0)}] {problem.get('host') or 'unknown-host'} :: {problem.get('name')}"
        )

    for target in ("grafana", "zabbix", "kuma"):
        status = (dashboards.get(target) or {}).get("status", "unknown")
        lines.append(f"- {target.title()} dashboard: {status}")
        reason = (dashboards.get(target) or {}).get("frame_reason")
        if reason:
            lines.append(f"  * {reason}")

    return "\n".join(lines)
