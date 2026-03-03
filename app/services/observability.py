"""Shared observability target config and lightweight dashboard health checks."""

from __future__ import annotations

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
