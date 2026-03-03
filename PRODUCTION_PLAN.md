# NOC Sentinel v2 — Production Readiness Plan

> **Date:** 2026-02-28
> **Status:** Smoke test complete — issues ranked by real-world risk

---

## SMOKE TEST RESULTS

### What works correctly
- FastAPI startup, lifespan, DB pool initialization
- Session middleware wired correctly
- All routers registered and importable
- DB migration + employee seeding + instruction seeding (new)
- `stream_ai()` correctly routes to all 5 providers including OpenRouter
- Employee prompt assembly from 4 structured columns
- Workflow engine loads, APScheduler registers jobs
- Memory extraction wired for all providers
- `build_employee_system_prompt()` fallback chain works
- All 8 modified files pass syntax check cleanly

### Bugs found
| # | File | Issue | Severity |
|---|------|-------|----------|
| B1 | `routers/vault.py` | Any logged-in user (viewer) can read all secrets | Critical |
| B2 | `routers/workflows.py:114` | `test-webhook` has no URL validation → SSRF risk | Critical |
| B3 | `main.py:78` | `spa_fallback` path not normalized → path traversal | High |
| B4 | `main.py` | No `/api/health` endpoint → can't detect failures | High |
| B5 | `main.py` | No security headers (CSP, X-Frame-Options, HSTS) | High |
| B6 | `routers/auth.py` | No rate limiting on login → brute force possible | High |
| B7 | `config.py:10` | Default `app_secret` is a placeholder string | High |
| B8 | `routers/workflows.py` | `trigger_type` + `action_type` accept any string | Medium |
| B9 | `routers/workflows.py:125` | WhatsApp service URL hardcoded (not env var) | Medium |
| B10 | `services/memory.py:64` | `_OPENROUTER_HEADERS` defined inside function (minor) | Low |

---

## FIX PRIORITY

### PHASE 1 — Fix now (blocking production)

#### Fix B1: Vault access control
**File:** `app/routers/vault.py`
- `GET /api/vault` (list secrets): change `Depends(get_session)` → `Depends(require_operator)`
- `POST /api/vault`, `PUT /api/vault/{id}`, `DELETE /api/vault/{id}`: change to `Depends(require_operator)`
- Keep `share_with_ai` secret values masked in list response (show only first 4 chars)

#### Fix B2: SSRF on test-webhook
**File:** `app/routers/workflows.py:109`
```python
ALLOWED_WEBHOOK_SCHEMES = {"https", "http"}
BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254"}

@router.post("/test-webhook")
async def test_webhook(body: TestWebhookBody, session: dict = Depends(require_operator)):
    from urllib.parse import urlparse
    parsed = urlparse(body.url)
    if parsed.scheme not in ALLOWED_WEBHOOK_SCHEMES:
        raise HTTPException(400, "Only http/https URLs allowed")
    if parsed.hostname in BLOCKED_HOSTS:
        raise HTTPException(400, "Internal addresses not allowed")
    ...
```

#### Fix B3: Path traversal in spa_fallback
**File:** `app/main.py:78`
```python
file_path = os.path.normpath(os.path.join(static_dir, path))
if not file_path.startswith(static_dir):          # ← add this guard
    return JSONResponse({"error": "Not found"}, status_code=404)
if os.path.isfile(file_path):
    return FileResponse(file_path)
```

#### Fix B4: Health endpoint
**File:** `app/main.py` — add after router registration:
```python
@app.get("/api/health")
async def health():
    try:
        from app.database import fetch_one
        await fetch_one("SELECT 1")
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return JSONResponse({"status": "error", "db": str(e)}, status_code=503)
```

#### Fix B5: Security headers middleware
**File:** `app/main.py` — add after SessionMiddleware:
```python
from starlette.middleware.base import BaseHTTPMiddleware

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"]        = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"]     = "geolocation=(), camera=(), microphone=()"
        return response

app.add_middleware(SecurityHeadersMiddleware)
```

#### Fix B6: Login rate limiting
**File:** `app/routers/auth.py`
```python
# Simple in-memory rate limiter (replace with Redis in scaled deployment)
from collections import defaultdict
import time

_login_attempts: dict[str, list[float]] = defaultdict(list)
LOGIN_WINDOW  = 900   # 15 minutes
LOGIN_MAX     = 10    # max attempts per window

def _check_rate_limit(ip: str):
    now = time.time()
    attempts = [t for t in _login_attempts[ip] if now - t < LOGIN_WINDOW]
    _login_attempts[ip] = attempts
    if len(attempts) >= LOGIN_MAX:
        raise HTTPException(429, "Too many login attempts. Try again in 15 minutes.")
    _login_attempts[ip].append(now)

# In login endpoint:
@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    ip = request.client.host
    _check_rate_limit(ip)
    ...
```

#### Fix B7: Enforce app_secret strength
**File:** `app/config.py`
```python
@validator("app_secret")
def secret_must_be_strong(cls, v):
    if v == "change_this_to_a_random_string_at_least_32_chars_long":
        raise ValueError("APP_SECRET must be changed from the default value")
    if len(v) < 32:
        raise ValueError("APP_SECRET must be at least 32 characters")
    return v
```

---

### PHASE 2 — Fix before first client deployment

#### Fix B8: Validate trigger_type + action_type
**File:** `app/routers/workflows.py`
```python
from typing import Literal

class WorkflowBody(BaseModel):
    trigger_type: Literal["alarm", "schedule", "threshold", "manual"] = "manual"
    action_type:  Literal["log", "webhook", "zabbix_ack", "email", "teams", "whatsapp_group"] = "log"
    employee_id: str = "aria"

    @validator("employee_id")
    def valid_employee(cls, v):
        if v not in ("aria", "nexus", "cipher", "vega"):
            raise ValueError("employee_id must be one of: aria, nexus, cipher, vega")
        return v
```

#### Fix B9: WhatsApp URL from environment
**File:** `app/routers/workflows.py`
```python
import os
WA_SERVICE = os.getenv("WHATSAPP_SERVICE_URL", "http://localhost:3001")
```
Add to `.env.example`:
```
WHATSAPP_SERVICE_URL=http://localhost:3001
```

#### Fix B10: Move OpenRouter headers to module level
**File:** `app/services/memory.py` — move `_OPENROUTER_HEADERS` to module level (not inside function)

---

### PHASE 3 — Before scaling to multiple users/servers

- Replace in-memory login rate limiter with Redis (`slowapi` library)
- Add request ID middleware (correlation IDs for log tracing)
- Add audit log table: `INSERT INTO audit_log (user_id, action, resource, detail, ip)` on every write operation
- Pin all dependency versions in `requirements.txt`
- Add DB connection retry with exponential backoff
- Add `/api/ready` endpoint that checks DB + Zabbix connectivity

---

## AI EMPLOYEE WORKFLOW INSTRUCTIONS — Plain Language System

### Design Principle
The workflow `prompt_template` is the **task brief** — written in plain English. It tells the employee WHAT to do and what context they have. The employee's instructions (from `employee_profiles`) tell them WHO they are and HOW to respond.

### Template Variables Available
```
{alarm_name}  → name of the triggering Zabbix alarm
{host}        → hostname of the affected device
{severity}    → numeric severity (0-5)
```

### Template Library — copy-paste ready

---

#### ARIA — NOC Analyst

**Alarm Triage (trigger_type: alarm)**
```
A new alarm has fired: "{alarm_name}" on host {host} with severity {severity}/5.

Triage this alarm immediately:
1. Assess whether this is customer-impacting — does it affect any payment flow?
2. Check if this is a known recurring issue or a new pattern
3. Determine if it requires escalation or can be resolved at NOC level
4. Provide your recommended immediate action in the next 5 minutes
5. Draft a brief Teams notification if severity is 4 or 5

Be specific. Use your NOC experience. Don't guess — ask for data if needed.
```

**Morning Shift Check (trigger_type: schedule, cron: 0 7 * * *)**
```
It's the start of the morning shift. Perform your shift handover check:

1. Summarize overnight alarm activity — what fired, what was resolved, what's still open
2. Flag any alarms that have been open for more than 2 hours without acknowledgment
3. Identify the top 3 risks entering this shift
4. List your priority actions for the next 4 hours
5. Note any patterns worth escalating to NEXUS or CIPHER

This briefing goes to the morning team. Be direct, specific, and actionable.
```

**Evening SLA Report (trigger_type: schedule, cron: 0 17 * * *)**
```
Generate today's SLA and uptime summary for Tabadul management:

1. How many critical/high alarms fired today?
2. Were there any payment-impacting outages? Duration and affected systems?
3. What was the average acknowledgment time?
4. Are we on track for 99.99% monthly SLA?
5. Top 3 recommended actions to improve tomorrow

Keep it executive-friendly. Numbers and clear statements only.
```

---

#### NEXUS — Infrastructure Engineer

**Device Alarm Analysis (trigger_type: alarm)**
```
A network device has triggered an alarm: "{alarm_name}" on {host} (severity {severity}/5).

Perform your infrastructure analysis:
1. Where does {host} sit in the network topology? What depends on it?
2. Is the HA pair healthy? Is there a failover risk right now?
3. What could cause this alarm — config change, hardware, capacity, external?
4. Is the standby device ready to take over if this device fails completely?
5. What is your recommended action? Include the exact CLI command or config step.
6. Rate the risk: Low / Medium / High / Critical

Be specific to the device. If you need interface stats or BGP state, ask for them.
```

**Weekly Capacity Review (trigger_type: schedule, cron: 0 9 * * 1)**
```
Perform the weekly infrastructure capacity review:

1. Which devices are approaching 80%+ utilization (CPU, memory, bandwidth)?
2. Are any ISP uplinks consistently near saturation?
3. Is the C6800 VSS switch fabric performing within normal range?
4. Any devices with increasing error counters or interface flaps this week?
5. Forecast: which capacity issues will become critical in the next 30 days?
6. Recommend 3 specific infrastructure improvements with effort estimates

Reference specific device names and interface statistics from Zabbix.
```

**Post-Maintenance Verification (trigger_type: manual)**
```
A maintenance window has just completed. Verify infrastructure health:

1. Check all HA pairs — are primary and standby both healthy?
2. Verify all ISP uplinks are up and BGP sessions are established
3. Check for any unexpected alarms that fired during the maintenance window
4. Confirm Zabbix is monitoring all critical devices
5. Run through the critical path: ISPs → C6800 → FortiGate → Firepower → PA-5250 → Payment servers
6. Give a Go/No-Go verdict with specific evidence

Be systematic. Check every link in the chain.
```

---

#### CIPHER — Security Analyst

**Security Alarm Triage (trigger_type: alarm)**
```
A security-relevant alarm has fired: "{alarm_name}" on {host} (severity {severity}/5).

Assess this alert immediately:
1. Is this a genuine threat or a false positive? What evidence supports your assessment?
2. MITRE ATT&CK: what tactic and technique does this match, if any?
3. What is the blast radius — what data or systems are at risk?
4. Does this affect the PCI-DSS cardholder data environment?
5. Recommended response: Contain / Monitor / Investigate / Escalate
6. If this is High or Critical: draft the Teams security alert message now

Classify severity: [INFO] / [LOW] / [MEDIUM] / [HIGH] / [CRITICAL]
```

**Weekly Security Posture Review (trigger_type: schedule, cron: 0 8 * * 1)**
```
Perform the weekly security posture review for Tabadul's payment network:

1. Review FortiGate, Firepower, and PA-5250 alarm patterns this week — any anomalies?
2. Were there any IPS/IDS signature hits? Any blocked attack attempts worth noting?
3. Check firewall rule changes — were any rules modified this week? Are they justified?
4. PCI-DSS status: any compliance gaps or controls that need attention?
5. External threat intel: any known threats targeting financial infrastructure in the region?
6. Top 3 security improvements with priority (Critical / High / Medium) and effort (Low / Medium / High)
```

**Incident Security Assessment (trigger_type: manual)**
```
A potential security incident requires assessment. Analyze the current situation:

1. What indicators of compromise are present? List specific IOCs.
2. What is the attack vector? How did this happen?
3. What systems are confirmed affected vs possibly affected?
4. Immediate containment actions — what must be done in the next 15 minutes?
5. Evidence preservation — what logs and packet captures should we collect now?
6. Who needs to be notified? (Management, PCI-DSS QSA, law enforcement?)
7. Draft a brief incident status update for management

Blameless focus — what happened in the system, not who made mistakes.
```

---

#### VEGA — Site Reliability Engineer

**Reliability Check Post-Incident (trigger_type: alarm)**
```
An alarm has fired that may indicate a reliability issue: "{alarm_name}" on {host} (severity {severity}/5).

Assess reliability impact:
1. Is this affecting our SLO? What percentage of error budget has this consumed?
2. Is this a known failure mode we have a runbook for? If yes, which one?
3. If no runbook exists — flag this immediately as a monitoring gap
4. What monitoring coverage should we add to catch this faster next time?
5. MTTR assessment: how long until resolution based on current data?
6. Is this a recurring pattern? Check if we've seen this alarm before in the last 30 days

Always quantify: minutes of downtime, % availability, error budget impact.
```

**Daily Reliability Dashboard (trigger_type: schedule, cron: 0 8 * * *)**
```
Generate today's reliability status report:

1. Error budget status: how much of this month's budget remains?
   (Target SLO: 99.99% — error budget: ~4.4 minutes/month)
2. What was yesterday's actual uptime across payment flows?
3. Are there any recurring alarms (3+ occurrences this week) that indicate a systemic problem?
4. Which Zabbix alerts fired but had no corresponding runbook? Flag as gaps.
5. TOIL log: any manual operations repeated more than twice this week that should be automated?
6. Reliability verdict: ON TRACK / AT RISK / BREACHED — with specific evidence

Use numbers. "Yesterday's uptime was 99.97% — 4.3 minutes of degraded service on ISP-02."
```

**DR Test Report (trigger_type: manual)**
```
A disaster recovery test has just been completed. Document the results:

1. What was tested? (failover scenario, affected systems, time window)
2. RTO achieved vs target: how long did failover take? What is our target?
3. RPO achieved vs target: what data window was lost, if any?
4. What worked correctly? Be specific — which failover mechanisms activated as expected?
5. What failed or was slower than expected? No blame — systemic analysis only.
6. Runbook gaps: which steps were unclear, missing, or took longer than documented?
7. Top 3 improvements to the DR procedure before the next test

Format this as a proper DR test report. Management may read this.
```

---

### Workflow Configuration Reference

#### Trigger Config JSON format

**Schedule trigger:**
```json
{"cron": "0 8 * * *"}
```
Cron format: `minute hour day month weekday`
- Every day at 7am: `0 7 * * *`
- Every Monday at 9am: `0 9 * * 1`
- Every 30 minutes: `*/30 * * * *`

**Alarm trigger:**
```json
{
  "severity_min": 4,
  "host_filter": "firewall"
}
```
- `severity_min`: 0=Any, 2=Warning, 3=Average, 4=High, 5=Disaster only
- `host_filter`: optional substring match on alarm name (empty = all alarms)

#### Action Config JSON format

**Webhook:**
```json
{"url": "https://your-n8n-instance/webhook/xxx"}
```

**Email:**
```json
{
  "to": "noc-team@tabadul.iq",
  "subject": "NOC Sentinel Alert — {alarm_name}",
  "emp_id": "aria"
}
```

**Teams:**
```json
{
  "webhook_url": "https://outlook.office.com/webhook/...",
  "title": "NOC Alert",
  "emp_id": "cipher"
}
```

**WhatsApp group:**
```json
{
  "group_jid": "120363xxxxx@g.us",
  "emp_id": "aria"
}
```

**Zabbix acknowledge:**
```json
{}
```
(No config needed — auto-acknowledges the triggering alarm)

---

## PRODUCTION DEPLOYMENT CHECKLIST

### Pre-deployment
- [ ] Generate strong `APP_SECRET` (min 64 chars): `python3 -c "import secrets; print(secrets.token_hex(32))"`
- [ ] Set all required env vars in `.env`
- [ ] Create DB user with only SELECT/INSERT/UPDATE/DELETE (no DROP/CREATE)
- [ ] Run DB migrations in dry-run mode first
- [ ] Verify all 5 AI provider keys work (run test call)
- [ ] Test Zabbix API connectivity
- [ ] Test M365 Graph API (send test email)

### Infrastructure
- [ ] Put behind nginx reverse proxy (handles HTTPS termination)
- [ ] Set `https_only=True` in SessionMiddleware (after nginx is confirmed handling HTTPS)
- [ ] Configure nginx to pass `X-Forwarded-For` and `X-Real-IP`
- [ ] Set `TRUST_PROXY_HEADERS=true` in env if behind nginx
- [ ] Set up systemd service for FastAPI process
- [ ] Set up PM2 for WhatsApp Node.js service
- [ ] Configure firewall: only port 80/443 public, 8000 only from nginx

### Post-deployment verification
- [ ] `GET /api/health` returns `{"status": "ok", "db": "connected"}`
- [ ] Login works, session persists across pages
- [ ] Zabbix status loads on dashboard
- [ ] Chat with Aria responds (test all 5 providers)
- [ ] Create and manually trigger a test workflow
- [ ] Vault create/read/delete works (admin only)
- [ ] Employee instructions load correctly
- [ ] WhatsApp QR scan works (if using WA integration)

### Monitoring
- [ ] Set up external uptime monitor on `/api/health`
- [ ] Configure Zabbix to monitor the NOC Sentinel server itself (dogfooding)
- [ ] Set log rotation for uvicorn logs
- [ ] Alert on `/api/health` returning 503

---

## NGINX CONFIG TEMPLATE (production)

```nginx
server {
    listen 443 ssl http2;
    server_name noc.tabadul.iq;

    ssl_certificate     /etc/ssl/certs/tabadul.crt;
    ssl_certificate_key /etc/ssl/private/tabadul.key;
    ssl_protocols       TLSv1.2 TLSv1.3;

    # Security headers
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;

    # SSE needs special buffering settings
    location /api/chat {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Connection '';
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 300s;
    }

    location /api/office/run {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Connection '';
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 300s;
    }

    location /api/office/collaborate {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Connection '';
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 300s;
    }

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}

server {
    listen 80;
    server_name noc.tabadul.iq;
    return 301 https://$host$request_uri;
}
```

---

*End of Production Plan*
