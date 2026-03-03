"""
Employee memory system.
After each completed task, extract learnings and store them.
On the next task, inject past memories into the system prompt.
"""
import json
import logging
import httpx
from app.database import fetch_all, execute

logger = logging.getLogger(__name__)

MAX_MEMORY_INJECT = 8   # how many past memories to inject per task
MAX_MEMORY_STORE  = 200  # keep at most this many per employee

_OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://noc-sentinel.tabadul",
    "X-Title": "NOC Sentinel",
}


async def get_memory_context(employee_id: str) -> str:
    """Fetch recent memories and format them for system prompt injection."""
    memories = await fetch_all(
        "SELECT task_summary, key_learnings FROM employee_memory "
        "WHERE employee_id = %s ORDER BY created_at DESC LIMIT %s",
        (employee_id, MAX_MEMORY_INJECT),
    )
    if not memories:
        return ""

    lines = []
    for m in memories:
        summary = m.get("task_summary") or ""
        learnings = m.get("key_learnings") or ""
        if summary or learnings:
            lines.append(f"- Task: {summary}\n  Learned: {learnings}")

    if not lines:
        return ""

    return "\n\nPAST EXPERIENCE (what you've learned from previous tasks):\n" + "\n".join(lines)


async def save_memory(
    employee_id: str,
    task_type: str,
    task_prompt: str,
    ai_response: str,
    api_key: str,
    provider: str = "claude",
    model: str = "claude-haiku-4-5-20251001",
) -> None:
    """
    After a task completes, ask the AI to summarise it and extract learnings.
    Store the result in employee_memory.
    """
    if not ai_response or len(ai_response) < 50:
        return

    extraction_prompt = (
        "Summarise the following AI-generated work in 1 short sentence (max 120 chars). "
        "Then list 2-3 key technical learnings or findings as a comma-separated list.\n\n"
        f"TASK: {task_prompt[:300]}\n\nRESPONSE EXCERPT:\n{ai_response[:1500]}\n\n"
        "Reply in EXACTLY this JSON format (no markdown, no extra text):\n"
        '{"summary": "...", "learnings": "..."}'
    )

    result = None
    try:
        if provider == "claude":
            result = await _call_claude(api_key, model, extraction_prompt)
        elif provider in ("openai", "grok"):
            url = "https://api.openai.com/v1/chat/completions" if provider == "openai" else "https://api.x.ai/v1/chat/completions"
            result = await _call_openai_compat(api_key, model, url, extraction_prompt)
        elif provider == "openrouter":
            result = await _call_openai_compat(
                api_key, model,
                "https://openrouter.ai/api/v1/chat/completions",
                extraction_prompt,
                extra_headers=_OPENROUTER_HEADERS,
            )
        elif provider == "gemini":
            result = await _call_gemini(api_key, model, extraction_prompt)
    except Exception as e:
        logger.warning(f"Memory extraction failed: {e}")
        return

    if not result:
        return

    try:
        data = json.loads(result)
        summary   = str(data.get("summary", ""))[:500]
        learnings = str(data.get("learnings", ""))[:1000]
    except Exception:
        # Try to parse partial response
        summary   = task_prompt[:120]
        learnings = ai_response[:200]

    await execute(
        "INSERT INTO employee_memory (employee_id, task_type, task_summary, key_learnings) VALUES (%s,%s,%s,%s)",
        (employee_id, task_type, summary, learnings),
    )

    # Prune old memories beyond MAX_MEMORY_STORE
    await execute(
        "DELETE FROM employee_memory WHERE employee_id = %s AND id NOT IN "
        "(SELECT id FROM (SELECT id FROM employee_memory WHERE employee_id = %s "
        "ORDER BY created_at DESC LIMIT %s) t)",
        (employee_id, employee_id, MAX_MEMORY_STORE),
    )


async def save_memory_direct(
    employee_id: str,
    task_type: str,
    task_summary: str,
    key_learnings: str,
    host: str = None,
    alarm_type: str = None,
    source: str = "auto",
    weight: int = 1,
) -> None:
    """
    Save a memory entry without a secondary AI summarisation call.
    Use this for workflow runs, peer messages, and other automated contexts
    where the summary and learnings are already known.
    F11: auto-tags with day_of_week and hour_of_day for pattern recognition.
    """
    if not task_summary and not key_learnings:
        return
    from datetime import datetime
    now = datetime.utcnow()
    try:
        await execute(
            "INSERT INTO employee_memory "
            "(employee_id, task_type, task_summary, key_learnings, "
            " host, alarm_type, day_of_week, hour_of_day, source, weight) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                employee_id,
                task_type,
                str(task_summary)[:500],
                str(key_learnings)[:1000],
                host,
                alarm_type,
                now.weekday(),      # 0=Mon, 6=Sun  (Python convention)
                now.hour,
                source,
                weight,
            ),
        )
        # Prune old memories
        await execute(
            "DELETE FROM employee_memory WHERE employee_id = %s AND id NOT IN "
            "(SELECT id FROM (SELECT id FROM employee_memory WHERE employee_id = %s "
            "ORDER BY created_at DESC LIMIT %s) t)",
            (employee_id, employee_id, MAX_MEMORY_STORE),
        )
    except Exception as e:
        logger.warning(f"save_memory_direct({employee_id}) failed: {e}")


async def get_pattern_context(employee_id: str, host: str = None, alarm_type: str = None) -> str:
    """
    F11 — Pattern Recognition.
    Returns a formatted string of time-based patterns for this host/alarm type.
    Injected into prompts when an alarm fires on a known host.
    """
    if not host and not alarm_type:
        return ""

    from datetime import datetime
    now = datetime.utcnow()
    dow = now.weekday()
    hod = now.hour

    params: list = [employee_id]
    sql = (
        "SELECT task_summary, key_learnings, day_of_week, hour_of_day, COUNT(*) as occurrences "
        "FROM employee_memory WHERE employee_id=%s"
    )
    if host:
        sql += " AND host=%s"; params.append(host)
    if alarm_type:
        sql += " AND alarm_type=%s"; params.append(alarm_type)
    sql += " GROUP BY day_of_week, hour_of_day, task_summary, key_learnings ORDER BY occurrences DESC LIMIT 5"

    patterns = await fetch_all(sql, params)
    if not patterns:
        return ""

    # Find time-matching patterns (same day of week ±1 or same hour ±2)
    matched, generic = [], []
    for p in patterns:
        p_dow = p.get("day_of_week")
        p_hod = p.get("hour_of_day")
        time_match = (
            (p_dow is not None and abs(p_dow - dow) <= 1) or
            (p_hod is not None and abs(p_hod - hod) <= 2)
        )
        if time_match:
            matched.append(p)
        else:
            generic.append(p)

    lines = []
    if matched:
        lines.append("TIME-BASED PATTERNS (you've seen this at similar times before):")
        for p in matched[:3]:
            dow_name = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][p["day_of_week"]] if p.get("day_of_week") is not None else "?"
            lines.append(f"  [{dow_name} ~{p.get('hour_of_day','?')}:00 | {p['occurrences']}x] {p.get('task_summary','')}")
            if p.get("key_learnings"):
                lines.append(f"    → {p['key_learnings'][:200]}")

    if generic and not matched:
        lines.append("KNOWN PATTERNS for this host/alarm:")
        for p in generic[:2]:
            lines.append(f"  [{p['occurrences']}x] {p.get('task_summary','')}")
            if p.get("key_learnings"):
                lines.append(f"    → {p['key_learnings'][:200]}")

    return "\n".join(lines) if lines else ""


async def get_memories(employee_id: str, limit: int = 50) -> list[dict]:
    return await fetch_all(
        "SELECT id, task_type, task_summary, key_learnings, created_at "
        "FROM employee_memory WHERE employee_id = %s ORDER BY created_at DESC LIMIT %s",
        (employee_id, limit),
    )


# ── Mini AI callers for extraction (non-streaming) ────────────────────────────

async def _call_claude(key: str, model: str, prompt: str) -> str | None:
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            json={"model": model, "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]},
        )
        data = resp.json()
        return data.get("content", [{}])[0].get("text")


async def _call_openai_compat(
    key: str, model: str, url: str, prompt: str, extra_headers: dict = None
) -> str | None:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.post(
            url,
            headers=headers,
            json={"model": model, "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]},
        )
        data = resp.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content")


async def _call_gemini(key: str, model: str, prompt: str) -> str | None:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.post(
            url,
            headers={"Content-Type": "application/json"},
            json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                  "generationConfig": {"maxOutputTokens": 300}},
        )
        data = resp.json()
        return data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text")
