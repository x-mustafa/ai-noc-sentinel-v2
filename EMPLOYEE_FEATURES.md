# AI Employee — Real Employee Behavior Features

> The gap between "a chatbot that answers questions" and "an employee you can rely on."
> Each feature below closes a specific behavioral gap.

---

## THE FUNDAMENTAL GAPS

| Real Employee Behavior | Current State | Gap |
|------------------------|---------------|-----|
| Proactively notices things | Waits to be triggered | Passive |
| Owns incidents start-to-finish | Fires-and-forgets on workflows | No ownership |
| Remembers specific devices' quirks | Generic task memory | No device-level memory |
| Knows what shift they're on | No time awareness | Stateless |
| Has a to-do list and prioritizes | One task at a time | No queue |
| Messages colleagues directly | Formal team sessions only | No async messaging |
| Knows if their advice worked | No outcome tracking | No feedback loop |
| Writes and updates runbooks | Generic memory blobs | No runbook system |
| Escalates and follows up | Sends a message and stops | No escalation tracking |
| Improves their own tools | Can't modify workflows | No self-improvement |

---

## FEATURE CATALOG

---

### F1 — SHIFT SYSTEM
**Gap it closes:** Real employees know when their shift starts, what happened before them, and what they need to hand off.
**Priority:** P1 — This changes everything. Without it, employees have no temporal awareness.

**How it works:**
- Each employee has a configurable shift schedule (morning/evening/night or custom)
- On shift start → employee auto-runs a "shift brief" pulling the last 8 hours of events
- On shift end → employee auto-generates a shift handover report and sends to Teams/email
- Employees flag items as "needs attention next shift"
- Memory injection includes "what happened in the last shift" context

**DB tables needed:**
```sql
CREATE TABLE shift_config (
  employee_id  VARCHAR(20) PRIMARY KEY,
  shift_start  VARCHAR(5) DEFAULT '07:00',  -- HH:MM
  shift_end    VARCHAR(5) DEFAULT '15:00',
  timezone     VARCHAR(50) DEFAULT 'Asia/Baghdad',
  enabled      TINYINT(1) DEFAULT 1
);

CREATE TABLE shift_handover (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  employee_id  VARCHAR(20) NOT NULL,
  shift_date   DATE NOT NULL,
  shift_type   VARCHAR(20),         -- morning/evening/night
  briefing     LONGTEXT,            -- what happened
  watch_items  TEXT,                -- JSON: list of things to watch
  status       ENUM('active','closed') DEFAULT 'active',
  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**API endpoints:**
- `GET  /api/office/shift/{employee_id}` — get current shift status
- `POST /api/office/shift/{employee_id}/start` — manually start shift (also auto-triggered by cron)
- `POST /api/office/shift/{employee_id}/end` — generate handover and close shift
- `GET  /api/office/shift/{employee_id}/handover` — get last handover report

**System prompt injection:**
```
SHIFT CONTEXT:
Your shift started at 07:00 today. You are the morning NOC Analyst.
Previous shift handover notes:
  - ISP-02 (Passport-SS) had 3 flaps between 02:00-04:00. Still unstable.
  - Firepower IPS triggered 47 times on signature 2024-01-XXXX. CIPHER is investigating.
  - Payment gateway VISA-GW-01 SLA breach risk: at 99.95% for the month.
Watch items from previous shift: [ISP-02 stability, VISA-GW-01 latency]
```

---

### F2 — INCIDENT OWNERSHIP
**Gap it closes:** Real employees own an incident. They track it, update it, close it, write the RCA.
**Priority:** P1 — Without this, there's no accountability or continuity across tasks.

**How it works:**
- An "incident" is created when a critical alarm fires or a human creates one
- An employee is assigned as incident owner
- The owner tracks all updates to the incident
- The system periodically asks the owner "any updates on incident #X?"
- When resolved, the owner writes the RCA and it becomes a memory entry
- Incidents have SLA timers — owner gets notified if SLA is at risk

**DB tables needed:**
```sql
CREATE TABLE incidents (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  title           VARCHAR(300) NOT NULL,
  description     TEXT,
  owner_id        VARCHAR(20),        -- employee_id
  severity        TINYINT DEFAULT 3,  -- 1-5 Zabbix scale
  status          ENUM('open','investigating','resolved','closed') DEFAULT 'open',
  zabbix_event_id VARCHAR(50),        -- link to Zabbix alarm
  host            VARCHAR(200),
  started_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  resolved_at     TIMESTAMP NULL,
  rca             LONGTEXT,           -- root cause analysis
  created_by      VARCHAR(50)         -- 'system' or username
);

CREATE TABLE incident_updates (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  incident_id   INT NOT NULL,
  employee_id   VARCHAR(20),
  update_text   TEXT,
  update_type   ENUM('status','finding','action','escalation','resolution'),
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_inc (incident_id, created_at)
);
```

**API endpoints:**
- `POST /api/incidents` — create incident (manual or auto from alarm workflow)
- `GET  /api/incidents` — list open incidents
- `PUT  /api/incidents/{id}` — update status, add RCA
- `POST /api/incidents/{id}/update` — employee adds update
- `POST /api/incidents/{id}/assign/{employee_id}` — reassign owner
- `POST /api/incidents/{id}/ask-update` — trigger AI owner to provide current status

**System prompt injection (owner's perspective):**
```
OPEN INCIDENTS YOU OWN:
  [INC-0047] ISP-02 BGP flapping — INVESTIGATING — started 2h ago
    Last update: "BGP session dropped 3 times. Suspecting route reflector issue."
    SLA: 4h to resolve — 2h remaining
  [INC-0051] VISA gateway latency spike — OPEN — assigned 30min ago
    No updates yet. This needs your attention.
```

---

### F3 — DEVICE / HOST KNOWLEDGE BASE
**Gap it closes:** Real employees know the quirks of each device. "That switch always drops ICMP during backup windows." Current memory is task-level, not device-level.
**Priority:** P1 — This is what makes an employee feel expert vs generic.

**How it works:**
- Every time an employee interacts with a specific host/device, they can save a "device note"
- The system auto-extracts device observations from task responses
- When a new alarm fires on a device, the employee's device notes for that host are injected into the prompt
- Notes have categories: known_issues, quirks, configuration, contacts, history

**DB tables needed:**
```sql
CREATE TABLE device_knowledge (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  employee_id  VARCHAR(20) NOT NULL,
  host         VARCHAR(200) NOT NULL,      -- hostname or IP
  zabbix_id    VARCHAR(50),
  category     ENUM('quirk','known_issue','config','contact','performance','security'),
  note         TEXT NOT NULL,
  confidence   TINYINT DEFAULT 3,          -- 1-5: how confident is the employee
  verified     TINYINT(1) DEFAULT 0,       -- human verified this note
  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_host (host, employee_id)
);
```

**API endpoints:**
- `GET  /api/office/knowledge/{employee_id}` — all device notes for an employee
- `GET  /api/office/knowledge/{employee_id}/host/{hostname}` — notes for a specific host
- `POST /api/office/knowledge/{employee_id}` — add a device note
- `PUT  /api/office/knowledge/{note_id}/verify` — human verifies a note

**System prompt injection (when alarm fires on host X):**
```
YOUR DEVICE KNOWLEDGE — {host}:
  [QUIRK] CPU always spikes to 85% during Zabbix discovery scans (every 6h). Not a real issue.
  [KNOWN_ISSUE] BGP session to ISP-02 drops when router rebooted — needs manual clear-ip bgp.
  [CONFIG] Primary interface: GigabitEthernet0/0/1. VLAN 100 is the management VLAN.
  [CONTACT] NOC contact at ISP-02: +964-XXX-XXXX (Mohammed). Available 8am-8pm Baghdad time.
```

---

### F4 — ASYNC PEER MESSAGING
**Gap it closes:** Real employees ping each other directly. "Hey Nexus, can you check if the BGP neighbor is up on ISP-01?" Currently only formal team sessions exist.
**Priority:** P1 — Critical for multi-domain incidents.

**How it works:**
- Any employee can send an async message to another employee
- Messages arrive in a queue; the recipient processes them on their next task run
- Messages can request a specific action: "analyze this", "check this", "what do you think about X"
- The reply goes back to the sender and to the human who initiated
- Humans can initiate employee-to-employee messages

**DB tables needed:**
```sql
CREATE TABLE employee_messages (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  from_employee VARCHAR(20) NOT NULL,
  to_employee   VARCHAR(20) NOT NULL,
  subject       VARCHAR(300),
  body          TEXT NOT NULL,
  context_data  TEXT,               -- JSON: incident_id, host, alarm, etc.
  status        ENUM('pending','processing','replied','dismissed') DEFAULT 'pending',
  reply         LONGTEXT,
  initiated_by  VARCHAR(100),       -- username or 'system'
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  replied_at    TIMESTAMP NULL,
  INDEX idx_to (to_employee, status)
);
```

**API endpoints:**
- `POST /api/office/messages` — send message from one employee to another
- `GET  /api/office/messages/{employee_id}/inbox` — pending messages for an employee
- `POST /api/office/messages/{id}/process` — trigger AI to reply to a message
- `GET  /api/office/messages/thread/{incident_id}` — all messages related to an incident

**Example use case:**
```
Human: "Aria, I need you to loop in Nexus on the ISP-02 issue."
Aria sends message to Nexus:
  FROM: ARIA
  TO: NEXUS
  SUBJECT: ISP-02 BGP stability — need infra perspective
  BODY: "Hey, I've been tracking ISP-02 flaps all morning. Seen 7 BGP drops since 02:00.
         Alarm pattern suggests it's the route reflector, not the line itself.
         Can you check the BGP neighbor state and the route reflector config?
         I'm opening INC-0047 and tagging you as collaborator."
```

---

### F5 — OUTCOME TRACKING / FEEDBACK LOOP
**Gap it closes:** Real employees know if their advice worked. They learn from failures. Currently the AI fires a recommendation into the void.
**Priority:** P1 — Without feedback, learning is blind.

**How it works:**
- When an employee makes a recommendation via a workflow, human can mark it: Acted/Correct, Acted/Incorrect, Ignored, Escalated
- The outcome is linked back to the original memory entry
- Over time, confidence scores build: "Aria's ISP recommendations have 87% correct outcome rate"
- Employee system prompt includes: "Your last 3 recommendations on ISP issues: 2 correct, 1 incorrect (you thought it was the ISP, it was the firewall)"

**DB tables needed:**
```sql
ALTER TABLE workflow_runs
  ADD COLUMN outcome ENUM('unknown','correct','incorrect','escalated','ignored') DEFAULT 'unknown',
  ADD COLUMN outcome_note TEXT,
  ADD COLUMN outcome_by VARCHAR(100),
  ADD COLUMN outcome_at TIMESTAMP NULL;

CREATE TABLE employee_performance (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  employee_id   VARCHAR(20) NOT NULL,
  task_type     VARCHAR(50),
  domain        VARCHAR(100),   -- 'isp', 'firewall', 'payment_gw', etc.
  correct_count INT DEFAULT 0,
  total_count   INT DEFAULT 0,
  updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_emp_domain (employee_id, task_type, domain)
);
```

**API endpoints:**
- `POST /api/workflows/{id}/runs/{run_id}/outcome` — submit outcome feedback
- `GET  /api/office/performance/{employee_id}` — accuracy stats by domain
- `GET  /api/office/performance/{employee_id}/history` — timeline of accuracy

**System prompt injection:**
```
YOUR RECENT PERFORMANCE CONTEXT:
  ISP-related recommendations: 14 total — 11 correct (78%), 2 incorrect, 1 escalated
  Your last incorrect call: Diagnosed BGP flap as ISP issue — root cause was local firewall rule.
  Firewall recommendations: 8 total — 8 correct (100%)
  This context should inform your confidence level in your current assessment.
```

---

### F6 — LIVING RUNBOOK SYSTEM
**Gap it closes:** Real employees write and maintain runbooks. When they solve a problem, they document it. Currently there's generic memory but no structured runbook.
**Priority:** P2

**How it works:**
- When an incident is resolved, employee is asked to draft a runbook entry
- Runbooks have: trigger condition, diagnosis steps, resolution steps, prevention steps, rollback
- Runbooks are queryable: "Vega, do we have a runbook for BGP neighbor drops?"
- Runbooks are injected into prompts when a matching alarm fires: "I have a runbook for this."
- Humans can edit and approve runbooks

**DB tables needed:**
```sql
CREATE TABLE runbooks (
  id             INT AUTO_INCREMENT PRIMARY KEY,
  title          VARCHAR(300) NOT NULL,
  author_id      VARCHAR(20),          -- employee_id
  trigger_desc   TEXT,                 -- "when to use this runbook"
  trigger_keywords VARCHAR(500),       -- comma-separated for matching
  symptoms       TEXT,                 -- what to look for
  diagnosis      LONGTEXT,             -- step by step diagnosis
  resolution     LONGTEXT,             -- step by step resolution
  prevention     TEXT,                 -- how to prevent recurrence
  rollback       TEXT,                 -- how to undo if something goes wrong
  estimated_mttr INT,                  -- estimated minutes to resolve
  last_tested    DATE,
  status         ENUM('draft','approved','deprecated') DEFAULT 'draft',
  related_hosts  TEXT,                 -- JSON array of affected host patterns
  created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  FULLTEXT INDEX ft_runbook (title, trigger_keywords, symptoms)
);
```

**API endpoints:**
- `POST /api/runbooks` — create runbook (human or AI)
- `GET  /api/runbooks` — list/search runbooks
- `POST /api/runbooks/match` — find matching runbook for an alarm/host
- `PUT  /api/runbooks/{id}/approve` — human approves a draft
- `POST /api/office/incidents/{id}/generate-runbook` — AI drafts runbook from resolved incident

**System prompt injection (when matching runbook exists):**
```
RUNBOOK AVAILABLE: "BGP Neighbor Drop — ISP Uplink Recovery"
  Author: NEXUS | Last tested: 2026-01-15 | Estimated MTTR: 12 minutes
  Trigger: BGP neighbor goes down on ISP uplinks
  Resolution summary: (1) Check physical link, (2) Verify BGP timers, (3) clear ip bgp neighbor...
  Full runbook: [available on request]
```

---

### F7 — PROACTIVE TREND WATCH
**Gap it closes:** Real employees notice trends before they become alarms. "ISP-02 packet loss has been creeping up for 3 days."
**Priority:** P2

**How it works:**
- Each employee has a "watchlist" of metrics/hosts they're monitoring
- The watchlist is automatically populated from incident history and device notes
- Every 4 hours, the employee runs a silent scan of their watchlist
- If a metric is trending toward a threshold, they proactively notify
- No alarm needed — trend detection before the alarm fires

**DB tables needed:**
```sql
CREATE TABLE watchlist (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  employee_id   VARCHAR(20) NOT NULL,
  host          VARCHAR(200),
  metric_key    VARCHAR(200),     -- Zabbix item key
  watch_reason  TEXT,
  threshold_pct INT DEFAULT 80,  -- alert when X% of threshold
  added_from    VARCHAR(100),    -- 'incident:42', 'manual', 'auto'
  is_active     TINYINT(1) DEFAULT 1,
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**API endpoints:**
- `GET  /api/office/watchlist/{employee_id}` — current watchlist
- `POST /api/office/watchlist/{employee_id}` — add to watchlist
- `DELETE /api/office/watchlist/{employee_id}/{id}` — remove from watchlist
- `POST /api/office/watchlist/{employee_id}/scan` — trigger immediate watchlist scan

---

### F8 — EMPLOYEE STATUS / AVAILABILITY
**Gap it closes:** Real employees have a status — available, busy, on-call, off-shift.
**Priority:** P2

**How it works:**
- Each employee has a real-time status: `available`, `busy`, `investigating`, `on_call`, `off_shift`
- Status changes automatically: busy when processing a task, investigating when owning an incident
- When an alarm fires and the assigned employee is `investigating`, the system decides: queue it, reassign, or interrupt
- Humans can see a "NOC board" showing all 4 employees and their current status + what they're working on

**DB column:**
```sql
ALTER TABLE employee_profiles
  ADD COLUMN status ENUM('available','busy','investigating','on_call','off_shift') DEFAULT 'available',
  ADD COLUMN current_task VARCHAR(500) DEFAULT NULL,
  ADD COLUMN status_since TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
```

**UI concept (NOC board):**
```
┌─────────────────────────────────────────────────────────────────┐
│  NOC SENTINEL — EMPLOYEE STATUS                                  │
│                                                                   │
│  [ARIA]          [NEXUS]         [CIPHER]        [VEGA]          │
│  ● INVESTIGATING  ● AVAILABLE     ● ON CALL       ● BUSY         │
│  INC-0047        —               24/7 security   Shift report    │
│  ISP-02 BGP      (2h idle)       rotation        generating...   │
│                                                                   │
│  Last seen: 4m   Last seen: 2h   On call since   Active now      │
└─────────────────────────────────────────────────────────────────┘
```

---

### F9 — SELF-IMPROVEMENT SUGGESTIONS
**Gap it closes:** Real employees suggest improvements to their own tools and processes.
**Priority:** P2

**How it works:**
- After 7 days of operation, each employee runs a weekly self-review
- They analyze: which workflows triggered most, which alarms had no runbook, which tasks took longest
- They generate a "improvements I suggest" report with specific action items
- Items can be: new workflow to create, runbook to write, Zabbix template to add, instruction to update

**System prompt for self-review:**
```
You are {employee}. Review the last 7 days of your work:

WORKFLOW RUNS (last 7 days): [inject summary]
UNMATCHED ALARMS (no runbook): [inject list]
INCIDENTS YOU OWNED: [inject list with outcomes]
YOUR ACCURACY: [inject performance stats]

Suggest 5 specific improvements:
1. New workflows to automate repeated work
2. Runbooks to write for unhandled scenarios
3. Instruction updates that would make you more accurate
4. Monitoring gaps in Zabbix you noticed
5. Anything else that would make you more effective

Be specific. Reference real alarm names, real hosts, real issues.
```

---

### F10 — ESCALATION OWNERSHIP
**Gap it closes:** Real employees own escalations. They follow up. They don't just send one message.
**Priority:** P2

**How it works:**
- When an employee escalates (to management, to vendor, to another team), it creates an "escalation record"
- The employee sets a follow-up timer (e.g., "expect response in 30 minutes")
- If no response by the timer, the employee automatically sends a follow-up
- Escalations are tracked to resolution

**DB tables needed:**
```sql
CREATE TABLE escalations (
  id             INT AUTO_INCREMENT PRIMARY KEY,
  incident_id    INT,
  employee_id    VARCHAR(20),
  escalated_to   VARCHAR(200),    -- 'management', 'ISP-vendor', 'Cisco-TAC', etc.
  channel        VARCHAR(50),     -- 'email', 'teams', 'phone', 'whatsapp'
  message_sent   TEXT,
  followup_at    TIMESTAMP,
  followup_count INT DEFAULT 0,
  status         ENUM('open','responded','closed') DEFAULT 'open',
  created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

### F11 — PATTERN RECOGNITION ACROSS TIME
**Gap it closes:** Real employees recognize time-based patterns. "Every Monday morning, ISP-02 has issues." Current memory is unindexed and time-unaware.
**Priority:** P2

**How it works:**
- Memory entries are tagged with: time-of-day, day-of-week, month, host, alarm-type
- When a new alarm fires, the employee checks: "Have I seen this type of alarm on this host at this time of week before?"
- If yes: inject that pattern insight into the prompt
- Pattern insights: "This alarm fires every Sunday around 2am — likely a scheduled task on the host"

**DB changes:**
```sql
ALTER TABLE employee_memory
  ADD COLUMN host VARCHAR(200) DEFAULT NULL,
  ADD COLUMN alarm_type VARCHAR(100) DEFAULT NULL,
  ADD COLUMN day_of_week TINYINT DEFAULT NULL,  -- 0=Sun, 6=Sat
  ADD COLUMN hour_of_day TINYINT DEFAULT NULL;

CREATE INDEX idx_pattern ON employee_memory (employee_id, host, alarm_type, day_of_week);
```

---

### F12 — HUMAN FEEDBACK INTERFACE
**Gap it closes:** Real employees get feedback from their manager. They learn from corrections.
**Priority:** P3

**How it works:**
- After any AI response (chat or workflow), human can rate it: ✓ Helpful / ✗ Wrong / ⚠ Partially right
- Human can add a correction note: "Actually the issue was X, not Y"
- Correction is stored as a high-priority memory entry: weight 3x normal
- Over time, builds an employee-specific "correction library" injected into prompts

**DB changes:**
```sql
ALTER TABLE employee_memory
  ADD COLUMN source ENUM('auto','human_correction','human_feedback') DEFAULT 'auto',
  ADD COLUMN weight TINYINT DEFAULT 1,     -- 1=normal, 3=correction, 5=critical
  ADD COLUMN feedback_from VARCHAR(100);

ALTER TABLE workflow_runs
  ADD COLUMN human_rating TINYINT DEFAULT NULL,   -- 1-5
  ADD COLUMN human_note TEXT DEFAULT NULL;
```

---

### F13 — CHANGE CALENDAR AWARENESS
**Gap it closes:** Real employees know about scheduled maintenance. They factor it into alarm analysis. "That BGP flap was expected — Nexus is doing maintenance on the core switch right now."
**Priority:** P3

**How it works:**
- Change calendar table: who, what, when, expected impact
- When an alarm fires during a scheduled change window, it's automatically tagged as "change-related"
- Employee's prompt includes: "ACTIVE MAINTENANCE: Nexus is upgrading FortiGate firmware until 22:00. Expect brief BGP flaps."

**DB tables needed:**
```sql
CREATE TABLE change_calendar (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  title           VARCHAR(300) NOT NULL,
  owner           VARCHAR(100),
  employee_id     VARCHAR(20),           -- which employee is doing it
  affected_hosts  TEXT,                  -- JSON: list of hostnames/IPs
  expected_impact VARCHAR(500),
  start_at        TIMESTAMP NOT NULL,
  end_at          TIMESTAMP NOT NULL,
  status          ENUM('planned','active','completed','cancelled') DEFAULT 'planned',
  notes           TEXT,
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

### F14 — SLA REAL-TIME TRACKER
**Gap it closes:** Real employees have SLA numbers in their head at all times. "We've used 87% of our error budget this month."
**Priority:** P1 for VEGA specifically

**How it works:**
- VEGA continuously tracks SLA state: calculates uptime % per payment path
- SLA tracker is injected into every VEGA response
- Alerts when error budget < 20%
- Monthly SLA report auto-generated at month-end

**DB tables needed:**
```sql
CREATE TABLE sla_tracker (
  id             INT AUTO_INCREMENT PRIMARY KEY,
  service        VARCHAR(200) NOT NULL,   -- 'VISA-GW', 'MASTER-GW', 'CBI-SWITCH'
  target_sla     DECIMAL(6,4) DEFAULT 99.99,
  month          DATE NOT NULL,           -- first day of month
  downtime_min   INT DEFAULT 0,           -- total downtime in minutes
  calculated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_service_month (service, month)
);

CREATE TABLE sla_events (
  id             INT AUTO_INCREMENT PRIMARY KEY,
  service        VARCHAR(200) NOT NULL,
  event_type     ENUM('outage_start','outage_end','degraded_start','degraded_end'),
  zabbix_event   VARCHAR(50),
  impact_note    VARCHAR(500),
  occurred_at    TIMESTAMP NOT NULL
);
```

---

### F15 — EMPLOYEE COLLABORATION INBOX (NOC BOARD)
**Gap it closes:** Real employees have a shared whiteboard — "here's what the NOC is dealing with right now."
**Priority:** P2

**How it works:**
- A shared, real-time view showing: open incidents, active escalations, each employee's status, recent workflow runs, SLA status
- Employees contribute to this automatically as they work
- Humans see the full picture without asking
- Acts as the "NOC situation display"

**Implementation:** SSE endpoint that streams real-time state changes from all employee activities.

---

## PRIORITY IMPLEMENTATION ORDER

```
MONTH 1 — Foundation (makes employees feel real)
  F2  Incident Ownership          ← biggest behavioral gap
  F1  Shift System                ← gives them temporal awareness
  F3  Device Knowledge Base       ← makes them feel expert
  F8  Employee Status / NOC Board ← humans can see what's happening

MONTH 2 — Collaboration & Learning
  F4  Async Peer Messaging        ← employees help each other
  F5  Outcome Tracking            ← feedback loop closes
  F6  Living Runbook System       ← captures institutional knowledge
  F14 SLA Real-time Tracker       ← VEGA becomes truly valuable

MONTH 3 — Intelligence & Autonomy
  F7  Proactive Trend Watch       ← stops being reactive-only
  F11 Pattern Recognition         ← time-aware intelligence
  F9  Self-Improvement Suggestions ← employees improve themselves
  F10 Escalation Ownership        ← closes the loop on critical events

MONTH 4 — Polish
  F12 Human Feedback Interface    ← manager/employee relationship
  F13 Change Calendar Awareness   ← removes noise from planned changes
  F15 NOC Board / Collaboration Inbox ← full situational awareness
```

---

## WHAT THIS LOOKS LIKE IN PRACTICE

### Before (current):
```
Human: "Aria, what's going on with ISP-02?"
Aria: [generic analysis of current alarm data]
Human: "Can you ask Nexus to check the BGP config?"
[Human manually starts a team session]
[Session happens]
[Nothing is recorded or followed up]
```

### After (all features):
```
08:00 — Aria's shift starts automatically.
        Aria reads the night shift handover: "ISP-02 had 7 BGP flaps. Watch closely."
        Aria adds ISP-02 to her watchlist. Status: AVAILABLE.

08:47 — ISP-02 BGP drops again. Alarm fires.
        Aria's device knowledge injects: "BGP always needs manual clear after reload."
        Aria creates INC-0052. Status: INVESTIGATING.
        Aria sends async message to Nexus: "BGP on ISP-02 again. Check route reflector config?"

09:02 — Nexus replies to Aria's message. Status: AVAILABLE → BUSY.
        Nexus checks BGP state, finds route reflector issue, documents it in device knowledge.
        Nexus replies: "Route reflector had stale route. Cleared. BGP stable."

09:04 — Aria marks INC-0052 resolved. Drafts runbook entry. Performance: +1 correct.
        Escalation timer cancelled (Nexus responded in time).
        Shift handover note added: "ISP-02 BGP resolved — route reflector. Nexus has runbook."

15:00 — Aria's shift ends. Generates handover report.
        Sends to Teams: "INC-0052 resolved. Runbook RB-0019 created. ISP-02 stable."
        Status: OFF_SHIFT.
```

This is the difference between a tool you query and a team you rely on.

---

## INSTRUCTION FORMAT FOR WORKFLOWS (PLAIN LANGUAGE)

Every workflow `prompt_template` must be written as if briefing a human colleague.
Structure: **Context → What I know → What I need from you → How to respond**

**Template pattern:**
```
[CONTEXT]
{alarm_name} fired on {host} at severity {severity}/5.

[WHAT YOU KNOW]
Check your device knowledge for {host}. Check if there's an active maintenance window.
Check if this alarm has fired in the last 24 hours (pattern check).

[WHAT I NEED]
1. Is this customer-impacting? (yes/no + reason)
2. Is this a known issue or something new?
3. Your recommended action in the next 10 minutes
4. Should this be escalated? To whom?

[HOW TO RESPOND]
Lead with: STATUS (CRITICAL/HIGH/NORMAL) + one sentence.
Then your analysis. Then your recommended action.
Under 200 words. No preamble.
```

This structure produces dramatically better AI responses than "analyze this alarm."
```
