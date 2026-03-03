import aiomysql
from app.config import settings
import logging

logger = logging.getLogger(__name__)

_pool: aiomysql.Pool | None = None


async def get_pool() -> aiomysql.Pool:
    global _pool
    if _pool is None:
        _pool = await aiomysql.create_pool(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_pass,
            db=settings.db_name,
            charset="utf8mb4",
            autocommit=True,
            minsize=2,
            maxsize=20,
        )
    return _pool


async def close_pool():
    global _pool
    if _pool:
        _pool.close()
        await _pool.wait_closed()
        _pool = None


async def fetch_one(sql: str, params=None) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(sql, params or ())
            return await cur.fetchone()


async def fetch_all(sql: str, params=None) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(sql, params or ())
            return await cur.fetchall()


async def execute(sql: str, params=None) -> int:
    """Returns lastrowid for INSERT, rowcount for UPDATE/DELETE."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params or ())
            return cur.lastrowid or cur.rowcount


async def execute_many(sql: str, params_list: list) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(sql, params_list)


async def run_migration():
    """Create new tables needed by Python version if they don't exist."""
    sqls = [
        """CREATE TABLE IF NOT EXISTS `employee_profiles` (
            `id` VARCHAR(20) PRIMARY KEY,
            `title` VARCHAR(100),
            `responsibilities` TEXT,
            `daily_tasks` TEXT,
            `system_prompt` TEXT,
            `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB""",

        """CREATE TABLE IF NOT EXISTS `employee_memory` (
            `id` INT AUTO_INCREMENT PRIMARY KEY,
            `employee_id` VARCHAR(20) NOT NULL,
            `task_type` VARCHAR(50),
            `task_summary` VARCHAR(500),
            `outcome_summary` TEXT,
            `key_learnings` TEXT,
            `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX `idx_emp_time` (`employee_id`, `created_at`)
        ) ENGINE=InnoDB""",

        """CREATE TABLE IF NOT EXISTS `workflows` (
            `id` INT AUTO_INCREMENT PRIMARY KEY,
            `name` VARCHAR(120) NOT NULL,
            `description` TEXT,
            `trigger_type` ENUM('alarm','schedule','threshold','manual') DEFAULT 'manual',
            `trigger_config` TEXT,
            `employee_id` VARCHAR(20),
            `prompt_template` TEXT,
            `action_type` ENUM('log','webhook','zabbix_ack','email') DEFAULT 'log',
            `action_config` TEXT,
            `is_active` TINYINT(1) DEFAULT 1,
            `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB""",

        """CREATE TABLE IF NOT EXISTS `workflow_runs` (
            `id` INT AUTO_INCREMENT PRIMARY KEY,
            `workflow_id` INT NOT NULL,
            `trigger_data` TEXT,
            `ai_response` TEXT,
            `action_result` TEXT,
            `status` ENUM('running','success','error') DEFAULT 'running',
            `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX `idx_wf` (`workflow_id`, `created_at`)
        ) ENGINE=InnoDB""",

        """CREATE TABLE IF NOT EXISTS `team_sessions` (
            `id` INT AUTO_INCREMENT PRIMARY KEY,
            `topic` VARCHAR(500),
            `participants` TEXT,
            `transcript` LONGTEXT,
            `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX `idx_ts_created` (`created_at`)
        ) ENGINE=InnoDB""",

        # Widen action_type to VARCHAR so new action types (whatsapp_group, etc.) can be stored
        """ALTER TABLE `workflows`
           MODIFY COLUMN `action_type` VARCHAR(30) DEFAULT 'log'""",

        # Widen further to TEXT to store JSON arrays for multi-action workflows
        """ALTER TABLE `workflows`
           MODIFY COLUMN `action_type` TEXT""",

        """CREATE TABLE IF NOT EXISTS `ms365_teams_webhooks` (
            `id` INT AUTO_INCREMENT PRIMARY KEY,
            `name` VARCHAR(100) NOT NULL UNIQUE,
            `webhook_url` TEXT NOT NULL,
            `channel` VARCHAR(100) DEFAULT '',
            `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB""",

        """CREATE TABLE IF NOT EXISTS `vault_entries` (
            `id` INT AUTO_INCREMENT PRIMARY KEY,
            `name` VARCHAR(200) NOT NULL,
            `category` VARCHAR(50) DEFAULT 'Other',
            `value` TEXT NOT NULL,
            `notes` TEXT,
            `share_with_ai` TINYINT(1) DEFAULT 1,
            `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB""",

        # OpenRouter provider key
        "ALTER TABLE `zabbix_config` ADD COLUMN IF NOT EXISTS `openrouter_key` VARCHAR(200) DEFAULT ''",

        # Structured instruction columns for AI employees
        "ALTER TABLE `employee_profiles` ADD COLUMN IF NOT EXISTS `instruction_identity` TEXT DEFAULT NULL",
        "ALTER TABLE `employee_profiles` ADD COLUMN IF NOT EXISTS `instruction_expertise` TEXT DEFAULT NULL",
        "ALTER TABLE `employee_profiles` ADD COLUMN IF NOT EXISTS `instruction_communication` TEXT DEFAULT NULL",
        "ALTER TABLE `employee_profiles` ADD COLUMN IF NOT EXISTS `instruction_constraints` TEXT DEFAULT NULL",

        # F8 — Employee Status (NOC Board)
        "ALTER TABLE `employee_profiles` ADD COLUMN IF NOT EXISTS `status` ENUM('available','busy','investigating','on_call','off_shift') DEFAULT 'available'",
        "ALTER TABLE `employee_profiles` ADD COLUMN IF NOT EXISTS `current_task` VARCHAR(500) DEFAULT NULL",
        "ALTER TABLE `employee_profiles` ADD COLUMN IF NOT EXISTS `status_since` TIMESTAMP DEFAULT CURRENT_TIMESTAMP",

        # F1 — Shift System
        """CREATE TABLE IF NOT EXISTS `shift_config` (
            `employee_id` VARCHAR(20) PRIMARY KEY,
            `shift_start` VARCHAR(5) DEFAULT '07:00',
            `shift_end`   VARCHAR(5) DEFAULT '15:00',
            `timezone`    VARCHAR(50) DEFAULT 'Asia/Baghdad',
            `enabled`     TINYINT(1) DEFAULT 1
        ) ENGINE=InnoDB""",

        """CREATE TABLE IF NOT EXISTS `shift_handover` (
            `id`          INT AUTO_INCREMENT PRIMARY KEY,
            `employee_id` VARCHAR(20) NOT NULL,
            `shift_date`  DATE NOT NULL,
            `shift_type`  VARCHAR(20),
            `briefing`    LONGTEXT,
            `watch_items` TEXT,
            `status`      ENUM('active','closed') DEFAULT 'active',
            `created_at`  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX `idx_sh_emp` (`employee_id`, `shift_date`)
        ) ENGINE=InnoDB""",

        # F2 — Incident Ownership
        """CREATE TABLE IF NOT EXISTS `incidents` (
            `id`              INT AUTO_INCREMENT PRIMARY KEY,
            `title`           VARCHAR(300) NOT NULL,
            `description`     TEXT,
            `owner_id`        VARCHAR(20),
            `severity`        TINYINT DEFAULT 3,
            `status`          ENUM('open','investigating','resolved','closed') DEFAULT 'open',
            `zabbix_event_id` VARCHAR(50),
            `host`            VARCHAR(200),
            `started_at`      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            `resolved_at`     TIMESTAMP NULL,
            `rca`             LONGTEXT,
            `created_by`      VARCHAR(50),
            INDEX `idx_inc_status` (`status`, `started_at`),
            INDEX `idx_inc_owner` (`owner_id`)
        ) ENGINE=InnoDB""",

        """CREATE TABLE IF NOT EXISTS `incident_updates` (
            `id`          INT AUTO_INCREMENT PRIMARY KEY,
            `incident_id` INT NOT NULL,
            `employee_id` VARCHAR(20),
            `update_text` TEXT,
            `update_type` ENUM('status','finding','action','escalation','resolution') DEFAULT 'finding',
            `created_at`  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX `idx_iu_inc` (`incident_id`, `created_at`)
        ) ENGINE=InnoDB""",

        # F3 — Device / Host Knowledge Base
        """CREATE TABLE IF NOT EXISTS `device_knowledge` (
            `id`          INT AUTO_INCREMENT PRIMARY KEY,
            `employee_id` VARCHAR(20) NOT NULL,
            `host`        VARCHAR(200) NOT NULL,
            `zabbix_id`   VARCHAR(50),
            `category`    ENUM('quirk','known_issue','config','contact','performance','security') DEFAULT 'known_issue',
            `note`        TEXT NOT NULL,
            `confidence`  TINYINT DEFAULT 3,
            `verified`    TINYINT(1) DEFAULT 0,
            `created_at`  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            `updated_at`  TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX `idx_dk_host` (`host`, `employee_id`)
        ) ENGINE=InnoDB""",

        # F5 — Outcome Tracking
        "ALTER TABLE `workflow_runs` ADD COLUMN IF NOT EXISTS `outcome` ENUM('unknown','correct','incorrect','escalated','ignored') DEFAULT 'unknown'",
        "ALTER TABLE `workflow_runs` ADD COLUMN IF NOT EXISTS `outcome_note` TEXT",
        "ALTER TABLE `workflow_runs` ADD COLUMN IF NOT EXISTS `outcome_by` VARCHAR(100)",
        "ALTER TABLE `workflow_runs` ADD COLUMN IF NOT EXISTS `outcome_at` TIMESTAMP NULL",

        """CREATE TABLE IF NOT EXISTS `employee_performance` (
            `id`            INT AUTO_INCREMENT PRIMARY KEY,
            `employee_id`   VARCHAR(20) NOT NULL,
            `task_type`     VARCHAR(50),
            `domain`        VARCHAR(100),
            `correct_count` INT DEFAULT 0,
            `total_count`   INT DEFAULT 0,
            `updated_at`    TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY `uk_emp_domain` (`employee_id`, `task_type`, `domain`)
        ) ENGINE=InnoDB""",

        # F4 — Async Peer Messaging
        """CREATE TABLE IF NOT EXISTS `employee_messages` (
            `id`            INT AUTO_INCREMENT PRIMARY KEY,
            `from_employee` VARCHAR(20) NOT NULL,
            `to_employee`   VARCHAR(20) NOT NULL,
            `subject`       VARCHAR(300),
            `body`          TEXT NOT NULL,
            `context_data`  TEXT,
            `status`        ENUM('pending','processing','replied','dismissed') DEFAULT 'pending',
            `reply`         LONGTEXT,
            `initiated_by`  VARCHAR(100),
            `created_at`    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            `replied_at`    TIMESTAMP NULL,
            INDEX `idx_em_to` (`to_employee`, `status`)
        ) ENGINE=InnoDB""",

        # F6 — Living Runbook System
        """CREATE TABLE IF NOT EXISTS `runbooks` (
            `id`               INT AUTO_INCREMENT PRIMARY KEY,
            `title`            VARCHAR(300) NOT NULL,
            `author_id`        VARCHAR(20),
            `trigger_desc`     TEXT,
            `trigger_keywords` VARCHAR(500),
            `symptoms`         TEXT,
            `diagnosis`        LONGTEXT,
            `resolution`       LONGTEXT,
            `prevention`       TEXT,
            `rollback`         TEXT,
            `estimated_mttr`   INT,
            `last_tested`      DATE,
            `status`           ENUM('draft','approved','deprecated') DEFAULT 'draft',
            `related_hosts`    TEXT,
            `created_at`       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            `updated_at`       TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB""",

        # F14 — SLA Real-time Tracker
        """CREATE TABLE IF NOT EXISTS `sla_tracker` (
            `id`            INT AUTO_INCREMENT PRIMARY KEY,
            `service`       VARCHAR(200) NOT NULL,
            `target_sla`    DECIMAL(6,4) DEFAULT 99.99,
            `month`         DATE NOT NULL,
            `downtime_min`  INT DEFAULT 0,
            `calculated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY `uk_service_month` (`service`, `month`)
        ) ENGINE=InnoDB""",

        """CREATE TABLE IF NOT EXISTS `sla_events` (
            `id`           INT AUTO_INCREMENT PRIMARY KEY,
            `service`      VARCHAR(200) NOT NULL,
            `event_type`   ENUM('outage_start','outage_end','degraded_start','degraded_end'),
            `zabbix_event` VARCHAR(50),
            `impact_note`  VARCHAR(500),
            `occurred_at`  TIMESTAMP NOT NULL,
            INDEX `idx_sla_svc` (`service`, `occurred_at`)
        ) ENGINE=InnoDB""",

        # Employee type (department/role template)
        "ALTER TABLE `employee_profiles` ADD COLUMN IF NOT EXISTS `employee_type` VARCHAR(50) DEFAULT 'noc_analyst'",

        # Employee Feedback — human comments on activity history events so AI can learn
        """CREATE TABLE IF NOT EXISTS `employee_feedback` (
            `id`          INT AUTO_INCREMENT PRIMARY KEY,
            `employee_id` VARCHAR(20) NOT NULL,
            `event_type`  VARCHAR(30) NOT NULL,
            `event_id`    INT NOT NULL,
            `comment`     TEXT NOT NULL,
            `rating`      TINYINT DEFAULT NULL COMMENT '1=wrong, 2=ok, 3=good',
            `created_by`  VARCHAR(100) DEFAULT 'operator',
            `created_at`  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX `idx_ef_emp` (`employee_id`, `event_type`, `event_id`)
        ) ENGINE=InnoDB""",

        # MS365 Settings — stored in DB so admins can set them via the UI
        "ALTER TABLE `zabbix_config` ADD COLUMN IF NOT EXISTS `ms365_tenant_id`           VARCHAR(200) DEFAULT ''",
        "ALTER TABLE `zabbix_config` ADD COLUMN IF NOT EXISTS `ms365_client_id`           VARCHAR(200) DEFAULT ''",
        "ALTER TABLE `zabbix_config` ADD COLUMN IF NOT EXISTS `ms365_client_secret`       VARCHAR(500) DEFAULT ''",
        "ALTER TABLE `zabbix_config` ADD COLUMN IF NOT EXISTS `ms365_email`               VARCHAR(200) DEFAULT ''",
        "ALTER TABLE `zabbix_config` ADD COLUMN IF NOT EXISTS `ms365_oauth_refresh_token` TEXT",
        "ALTER TABLE `zabbix_config` ADD COLUMN IF NOT EXISTS `ms365_oauth_access_token`  TEXT",
        "ALTER TABLE `zabbix_config` ADD COLUMN IF NOT EXISTS `ms365_oauth_token_expires` BIGINT DEFAULT 0",
        "ALTER TABLE `zabbix_config` ADD COLUMN IF NOT EXISTS `ms365_oauth_email`         VARCHAR(200) DEFAULT ''",

        # Per-employee AI provider/model override (null = use global default)
        "ALTER TABLE `employee_profiles` ADD COLUMN IF NOT EXISTS `ai_provider` VARCHAR(50)  DEFAULT NULL",
        "ALTER TABLE `employee_profiles` ADD COLUMN IF NOT EXISTS `ai_model`    VARCHAR(200) DEFAULT NULL",

        # Additional AI provider keys
        "ALTER TABLE `zabbix_config` ADD COLUMN IF NOT EXISTS `groq_key`     VARCHAR(200) DEFAULT ''",
        "ALTER TABLE `zabbix_config` ADD COLUMN IF NOT EXISTS `deepseek_key` VARCHAR(200) DEFAULT ''",
        "ALTER TABLE `zabbix_config` ADD COLUMN IF NOT EXISTS `mistral_key`  VARCHAR(200) DEFAULT ''",
        "ALTER TABLE `zabbix_config` ADD COLUMN IF NOT EXISTS `together_key` VARCHAR(200) DEFAULT ''",
        "ALTER TABLE `zabbix_config` ADD COLUMN IF NOT EXISTS `ollama_url`   VARCHAR(300) DEFAULT 'http://localhost:11434'",

        # Web-session providers (use Claude Pro / ChatGPT Plus subscription — no API billing)
        "ALTER TABLE `zabbix_config` ADD COLUMN IF NOT EXISTS `claude_web_session`  TEXT DEFAULT NULL",
        "ALTER TABLE `zabbix_config` ADD COLUMN IF NOT EXISTS `chatgpt_web_token`   TEXT DEFAULT NULL",

        # Audit log — every write action by any user
        """CREATE TABLE IF NOT EXISTS `audit_log` (
            `id`          INT AUTO_INCREMENT PRIMARY KEY,
            `user_id`     VARCHAR(100) NOT NULL,
            `method`      VARCHAR(10)  NOT NULL,
            `path`        VARCHAR(500) NOT NULL,
            `ip`          VARCHAR(45)  DEFAULT '',
            `status_code` SMALLINT     DEFAULT 0,
            `created_at`  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
            INDEX `idx_al_user` (`user_id`, `created_at`),
            INDEX `idx_al_time` (`created_at`)
        ) ENGINE=InnoDB""",

        # F7 — Proactive Trend Watch
        """CREATE TABLE IF NOT EXISTS `watchlist` (
            `id`           INT AUTO_INCREMENT PRIMARY KEY,
            `employee_id`  VARCHAR(20) NOT NULL,
            `host`         VARCHAR(200),
            `metric_key`   VARCHAR(200),
            `watch_reason` TEXT,
            `threshold_pct` INT DEFAULT 80,
            `added_from`   VARCHAR(100) DEFAULT 'manual',
            `is_active`    TINYINT(1) DEFAULT 1,
            `last_checked` TIMESTAMP NULL,
            `created_at`   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX `idx_wl_emp` (`employee_id`, `is_active`)
        ) ENGINE=InnoDB""",

        # F10 — Escalation Ownership
        """CREATE TABLE IF NOT EXISTS `escalations` (
            `id`             INT AUTO_INCREMENT PRIMARY KEY,
            `incident_id`    INT DEFAULT NULL,
            `employee_id`    VARCHAR(20) NOT NULL,
            `escalated_to`   VARCHAR(200) NOT NULL,
            `channel`        VARCHAR(50) DEFAULT 'teams',
            `message_sent`   TEXT,
            `followup_at`    TIMESTAMP NOT NULL,
            `followup_count` INT DEFAULT 0,
            `max_followups`  INT DEFAULT 3,
            `status`         ENUM('open','responded','closed') DEFAULT 'open',
            `response_note`  TEXT,
            `created_at`     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX `idx_esc_emp`    (`employee_id`, `status`),
            INDEX `idx_esc_timer`  (`followup_at`, `status`)
        ) ENGINE=InnoDB""",

        # F13 — Change Calendar
        """CREATE TABLE IF NOT EXISTS `change_calendar` (
            `id`              INT AUTO_INCREMENT PRIMARY KEY,
            `title`           VARCHAR(300) NOT NULL,
            `owner`           VARCHAR(100),
            `employee_id`     VARCHAR(20),
            `affected_hosts`  TEXT,
            `expected_impact` VARCHAR(500),
            `start_at`        TIMESTAMP NOT NULL,
            `end_at`          TIMESTAMP NOT NULL,
            `status`          ENUM('planned','active','completed','cancelled') DEFAULT 'planned',
            `notes`           TEXT,
            `created_at`      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX `idx_cc_time` (`start_at`, `end_at`, `status`)
        ) ENGINE=InnoDB""",

        # F11 — Pattern Recognition: tag memory entries with time metadata
        "ALTER TABLE `employee_memory` ADD COLUMN IF NOT EXISTS `host`        VARCHAR(200) DEFAULT NULL",
        "ALTER TABLE `employee_memory` ADD COLUMN IF NOT EXISTS `alarm_type`  VARCHAR(100) DEFAULT NULL",
        "ALTER TABLE `employee_memory` ADD COLUMN IF NOT EXISTS `day_of_week` TINYINT      DEFAULT NULL COMMENT '0=Sun,6=Sat'",
        "ALTER TABLE `employee_memory` ADD COLUMN IF NOT EXISTS `hour_of_day` TINYINT      DEFAULT NULL",

        # F9 — Self-Improvement: track last self-review per employee
        "ALTER TABLE `employee_profiles` ADD COLUMN IF NOT EXISTS `last_self_review` TIMESTAMP NULL",

        # F12 — Weighted feedback on memory entries
        "ALTER TABLE `employee_memory` ADD COLUMN IF NOT EXISTS `source` VARCHAR(30) DEFAULT 'auto'",
        "ALTER TABLE `employee_memory` ADD COLUMN IF NOT EXISTS `weight` TINYINT DEFAULT 1 COMMENT '1=normal,3=correction,5=critical'",

        # Incidents: auto-creation source tag
        "ALTER TABLE `incidents` ADD COLUMN IF NOT EXISTS `source` VARCHAR(50) DEFAULT 'manual'",

        # Alerting Rules Engine
        """CREATE TABLE IF NOT EXISTS `alert_rules` (
            `id`               INT AUTO_INCREMENT PRIMARY KEY,
            `name`             VARCHAR(200) NOT NULL,
            `enabled`          TINYINT(1) DEFAULT 1,
            `priority`         INT DEFAULT 0 COMMENT 'higher = evaluated first',
            `condition_field`  VARCHAR(50) NOT NULL COMMENT 'severity|host|alarm_name|tag',
            `condition_op`     VARCHAR(20) NOT NULL COMMENT '>=|<=|=|!=|contains|not_contains',
            `condition_value`  VARCHAR(200) NOT NULL,
            `action_type`      VARCHAR(50) NOT NULL COMMENT 'assign_employee|send_teams|send_email|suppress|create_incident',
            `action_data`      TEXT COMMENT 'JSON: {employee_id, webhook_url, email, ...}',
            `cooldown_minutes` INT DEFAULT 15,
            `fire_count`       INT DEFAULT 0,
            `last_fired`       TIMESTAMP NULL,
            `description`      TEXT,
            `created_at`       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX `idx_ar_enabled` (`enabled`, `priority`)
        ) ENGINE=InnoDB""",

        # Multi-site / Multi-tenant: additional Zabbix instances
        """CREATE TABLE IF NOT EXISTS `sites` (
            `id`          INT AUTO_INCREMENT PRIMARY KEY,
            `name`        VARCHAR(100) NOT NULL,
            `url`         VARCHAR(500) NOT NULL,
            `token`       VARCHAR(500) DEFAULT '',
            `username`    VARCHAR(100) DEFAULT '',
            `password`    VARCHAR(200) DEFAULT '',
            `color`       VARCHAR(20) DEFAULT '#00d4ff',
            `enabled`     TINYINT(1) DEFAULT 1,
            `is_default`  TINYINT(1) DEFAULT 0,
            `notes`       TEXT,
            `created_at`  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX `idx_sites_enabled` (`enabled`)
        ) ENGINE=InnoDB""",
    ]
    for sql in sqls:
        try:
            await execute(sql)
        except Exception as e:
            logger.warning(f"Migration step failed (may already exist): {e}")

    # Seed default employee profiles
    defaults = [
        ("aria",   "NOC Analyst",             "Alarm triage, incident lifecycle, SLA tracking, shift handover",
         '["Morning shift check","Review critical alarms","Shift handover briefing"]', None),
        ("nexus",  "Infrastructure Engineer", "Network devices, ISP uplinks, HA pairs, capacity planning, automation",
         '["Daily infrastructure health check","Device performance review","Capacity report"]', None),
        ("cipher", "Security Analyst",        "NGFW rules, IPS/IDS tuning, PCI-DSS compliance, threat hunting",
         '["Daily security posture review","Alarm pattern analysis","Threat assessment"]', None),
        ("vega",   "Site Reliability Engineer", "SLOs/SLIs, runbooks, monitoring gaps, DR testing, error budgets",
         '["Daily reliability review","Error budget estimate","Monitoring gap analysis"]', None),
    ]
    for emp_id, title, resp, daily_tasks, prompt in defaults:
        try:
            await execute(
                "INSERT IGNORE INTO employee_profiles (id, title, responsibilities, daily_tasks, system_prompt) VALUES (%s,%s,%s,%s,%s)",
                (emp_id, title, resp, daily_tasks, prompt),
            )
        except Exception:
            pass

    # Seed structured instruction columns (only if not yet set)
    from app.services.employee_prompt import seed_default_instructions
    await seed_default_instructions()

    # Seed default shift configs (one row per employee)
    for emp_id in ("aria", "nexus", "cipher", "vega"):
        try:
            await execute(
                "INSERT IGNORE INTO shift_config (employee_id) VALUES (%s)",
                (emp_id,),
            )
        except Exception:
            pass

    # Seed default SLA services for the current month
    import datetime as _dt
    _month = _dt.date.today().replace(day=1).isoformat()
    for _svc, _target in [
        ("VISA-GW", 99.99), ("MASTER-GW", 99.99), ("CBI-SWITCH", 99.99),
        ("ISP-SCOPESKY", 99.9), ("ISP-PASSPORT", 99.9),
    ]:
        try:
            await execute(
                "INSERT IGNORE INTO sla_tracker (service, target_sla, month) VALUES (%s,%s,%s)",
                (_svc, _target, _month),
            )
        except Exception:
            pass
