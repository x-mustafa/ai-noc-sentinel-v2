"""Shared observability target config and lightweight dashboard health checks."""

from __future__ import annotations

import re
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


def extract_kuma_status_text(html: str) -> str:
    text = str(html or "")
    text_lower = text.lower()
    for phrase in KUMA_STATUS_PHRASES:
        if phrase.lower() in text_lower:
            return phrase
    return ""


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
                "recent": True,
                "sortfield": "eventid",
                "sortorder": "DESC",
                "limit": 8,
            },
        )
        if isinstance(problems, list):
            top_problems = []
            for item in problems[:8]:
                top_problems.append(
                    {
                        "name": item.get("name", "Unknown problem"),
                        "severity": int(item.get("severity") or 0),
                        "host": "",
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
    summary = summarize_monitoring_sources(snapshot, zabbix, grafana, kuma)
    return {
        "snapshot": snapshot,
        "summary": summary,
        "sources": {
            "zabbix": zabbix,
            "grafana": grafana,
            "kuma": kuma,
        },
    }


def _status_rank(state: str) -> int:
    value = str(state or "").strip().lower()
    mapping = {
        "operational": 0,
        "up": 0,
        "ok": 0,
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

    grafana_status = "operational"
    if grafana.get("status") in {"error"}:
        grafana_status = "outage"
    elif grafana.get("auth_status") == "invalid_credentials" or grafana.get("dashboard_access") in {"embed_blocked", "login_required"}:
        grafana_status = "degraded"

    payload = {
        "source": "noc-sentinel",
        "derived_at": int(__import__("time").time()),
        "overall_status": _kuma_status_to_page_status(summary.get("recommended_kuma_state") or "up"),
        "headline": str(summary.get("headline") or ""),
        "recommended_kuma_state": str(summary.get("recommended_kuma_state") or "up"),
        "recommended_kuma_note": str(summary.get("recommended_kuma_note") or ""),
        "expires_in_seconds": 420,
        "groups": {
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
) -> dict:
    problem_count = int(zabbix.get("problem_count") or 0)
    abnormalities: list[str] = []
    actions: list[str] = []
    overall_status = "healthy"

    if str(zabbix.get("status") or "unknown") == "error":
        abnormalities.append("Zabbix API is unavailable.")
        overall_status = "critical"
    elif problem_count > 0:
        abnormalities.append(f"{problem_count} active Zabbix problem(s) detected.")
        overall_status = "critical" if problem_count >= 5 else "degraded"
        top_problem = ((zabbix.get("top_problems") or [])[:1] or [None])[0]
        if isinstance(top_problem, dict):
            actions.append(
                "Start with Zabbix: "
                + str(top_problem.get("host") or "unknown-host")
                + " -> "
                + str(top_problem.get("name") or "Unnamed problem")
            )

    grafana_api_health = str(grafana.get("api_health") or "unknown")
    grafana_status = str(grafana.get("status") or "unknown")
    if grafana_status == "error" or grafana_api_health not in {"ok", "unknown"}:
        abnormalities.append("Grafana API data is not fully available.")
        if overall_status != "critical":
            overall_status = "degraded"
    elif int(grafana.get("dashboard_count") or 0) > 0:
        titles = [
            str(item.get("title") or "")
            for item in (grafana.get("dashboards") or [])[:3]
            if isinstance(item, dict)
        ]
        if titles:
            actions.append("Grafana dashboards ready: " + ", ".join(titles))

    kuma_status = str(kuma.get("status") or "unknown")
    kuma_public_state = normalize_kuma_state(kuma.get("page_state") or kuma.get("status_text"))
    expected_kuma_state = _kuma_status_to_page_status(
        "major_outage" if overall_status == "critical"
        else "degraded_performance" if overall_status == "degraded"
        else "up"
    )
    kuma_alignment = "aligned"
    if kuma_status == "error":
        abnormalities.append("Kuma public status API is unavailable.")
        if overall_status != "critical":
            overall_status = "degraded"
        kuma_alignment = "unknown"
    elif kuma_public_state != "unknown" and expected_kuma_state != kuma_public_state:
        kuma_alignment = "mismatch"
        abnormalities.append(
            "Kuma public status is "
            + humanize_kuma_state(kuma_public_state)
            + " while NOC expects "
            + humanize_kuma_state(expected_kuma_state)
            + "."
        )
        if overall_status == "healthy":
            overall_status = "degraded"
    if int(kuma.get("problem_count") or 0) > 0:
        actions.append(f"Kuma public page already shows {int(kuma.get('problem_count') or 0)} public issue(s).")

    if not abnormalities:
        headline = "Monitoring sources are aligned and currently healthy."
    else:
        headline = abnormalities[0]

    recommended_kuma_state = (
        "major_outage" if overall_status == "critical"
        else "degraded_performance" if overall_status == "degraded"
        else "up"
    )
    recommended_kuma_note = " ".join((abnormalities or [headline])[:2])

    return {
        "overall_status": overall_status,
        "headline": headline,
        "abnormalities": abnormalities,
        "actions": actions,
        "recommended_kuma_state": recommended_kuma_state,
        "recommended_kuma_note": recommended_kuma_note,
        "problem_count": problem_count,
        "kuma_public_state": kuma_public_state,
        "kuma_alignment": kuma_alignment,
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
