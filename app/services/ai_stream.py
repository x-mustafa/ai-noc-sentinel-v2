"""
Async multi-provider SSE streaming for AI employees.
Supports: Claude (Anthropic), OpenAI, Gemini, Grok (xAI), OpenRouter
Each function is an async generator yielding SSE dicts for sse_starlette.
"""
import json
import uuid as _uuid_lib
import httpx
import logging
from typing import AsyncGenerator

from app.config import settings

# Cache org UUID per session-key prefix to avoid repeated /api/organizations calls
_claude_web_org_cache: dict = {}

logger = logging.getLogger(__name__)

SSE_DONE  = {"event": "done",  "data": "{}"}
TIMEOUT   = httpx.Timeout(120.0, connect=15.0)
OUTBOUND_TLS_VERIFY = settings.outbound_tls_verify


def _sse_text(text: str) -> dict:
    return {"data": json.dumps({"t": text}, ensure_ascii=False)}


def _sse_error(msg: str) -> dict:
    return {"event": "error", "data": json.dumps({"error": msg})}


def extract_text_chunk(chunk: dict) -> str:
    """Return the text payload from an SSE chunk, if present."""
    if not isinstance(chunk, dict):
        return ""
    raw = chunk.get("data")
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except Exception:
        return ""
    return str(data.get("t") or "")


def extract_error_chunk(chunk: dict) -> str:
    """Return the error payload from an SSE chunk, if present."""
    if not isinstance(chunk, dict) or chunk.get("event") != "error":
        return ""
    raw = chunk.get("data")
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except Exception:
        return ""
    return str(data.get("error") or "")


async def stream_claude(
    key: str, model: str, system: str, user_msg: str,
    images: list[dict] = None, history: list[dict] = None
) -> AsyncGenerator[dict, None]:
    images  = images  or []
    history = history or []
    user_content = []
    for img in images:
        user_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": img["type"], "data": img["data"]},
        })
    user_content.append({"type": "text", "text": user_msg})
    msg_content = user_msg if (not images and len(user_content) == 1) else user_content

    # Build messages array with history
    messages = []
    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": msg_content})

    payload = {
        "model":     model,
        "max_tokens": 4096,
        "stream":    True,
        "system":    system,
        "messages":  messages,
    }
    headers = {
        "Content-Type":      "application/json",
        "x-api-key":         key,
        "anthropic-version": "2023-06-01",
    }

    try:
        async with httpx.AsyncClient(verify=OUTBOUND_TLS_VERIFY, timeout=TIMEOUT) as client:
            async with client.stream("POST", "https://api.anthropic.com/v1/messages",
                                     headers=headers, json=payload) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield _sse_error(f"Claude API error {resp.status_code}: {body.decode()[:200]}")
                    return

                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        ev = json.loads(raw)
                    except Exception:
                        continue
                    if ev.get("type") == "content_block_delta":
                        text = ev.get("delta", {}).get("text", "")
                        if text:
                            yield _sse_text(text)
                    elif ev.get("type") == "message_stop":
                        break
                    elif ev.get("type") == "error":
                        yield _sse_error(ev.get("error", {}).get("message", "Stream error"))
                        return
    except Exception as e:
        yield _sse_error(f"Claude stream error: {e}")
        return

    yield SSE_DONE


async def stream_openai(
    key: str, model: str, system: str, user_msg: str,
    images: list[dict] = None, history: list[dict] = None,
    api_url: str = "https://api.openai.com/v1/chat/completions",
    extra_headers: dict = None,
) -> AsyncGenerator[dict, None]:
    images  = images  or []
    history = history or []
    user_content = []
    for img in images:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{img['type']};base64,{img['data']}"},
        })
    user_content.append({"type": "text", "text": user_msg})
    msg_content = user_msg if (not images and len(user_content) == 1) else user_content

    # Build messages with history
    messages = [{"role": "system", "content": system}]
    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": msg_content})

    payload = {
        "model":     model,
        "max_tokens": 4096,
        "stream":    True,
        "messages":  messages,
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    if extra_headers:
        headers.update(extra_headers)

    try:
        async with httpx.AsyncClient(verify=OUTBOUND_TLS_VERIFY, timeout=TIMEOUT) as client:
            async with client.stream("POST", api_url, headers=headers, json=payload) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield _sse_error(f"API error {resp.status_code}: {body.decode()[:200]}")
                    return

                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        ev = json.loads(raw)
                    except Exception:
                        continue
                    choice = ev.get("choices", [{}])[0]
                    text = choice.get("delta", {}).get("content", "")
                    if text:
                        yield _sse_text(text)
                    if choice.get("finish_reason") == "stop":
                        break
    except Exception as e:
        yield _sse_error(f"OpenAI stream error: {e}")
        return

    yield SSE_DONE


async def stream_grok(
    key: str, model: str, system: str, user_msg: str,
    images: list[dict] = None, history: list[dict] = None
) -> AsyncGenerator[dict, None]:
    async for chunk in stream_openai(
        key, model, system, user_msg, images, history,
        api_url="https://api.x.ai/v1/chat/completions",
    ):
        yield chunk


async def stream_openrouter(
    key: str, model: str, system: str, user_msg: str,
    images: list[dict] = None, history: list[dict] = None
) -> AsyncGenerator[dict, None]:
    async for chunk in stream_openai(
        key, model, system, user_msg, images, history,
        api_url="https://openrouter.ai/api/v1/chat/completions",
        extra_headers={
            "HTTP-Referer": "https://noc-sentinel.tabadul",
            "X-Title": "NOC Sentinel",
        },
    ):
        yield chunk


async def stream_gemini(
    key: str, model: str, system: str, user_msg: str,
    images: list[dict] = None, history: list[dict] = None
) -> AsyncGenerator[dict, None]:
    images  = images  or []
    history = history or []
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:streamGenerateContent?key={key}&alt=sse"
    )
    parts = []
    for img in images:
        parts.append({"inlineData": {"mimeType": img["type"], "data": img["data"]}})
    parts.append({"text": user_msg})

    # Build contents with history
    contents = []
    for m in history:
        role = "user" if m["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    contents.append({"role": "user", "parts": parts})

    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents":           contents,
        "generationConfig":   {"maxOutputTokens": 4096},
    }
    headers = {"Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(verify=OUTBOUND_TLS_VERIFY, timeout=TIMEOUT) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield _sse_error(f"Gemini API error {resp.status_code}: {body.decode()[:200]}")
                    return

                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    try:
                        ev = json.loads(raw)
                    except Exception:
                        continue
                    candidate = ev.get("candidates", [{}])[0]
                    text = candidate.get("content", {}).get("parts", [{}])[0].get("text", "")
                    if text:
                        yield _sse_text(text)
                    if candidate.get("finishReason") == "STOP":
                        break
    except Exception as e:
        yield _sse_error(f"Gemini stream error: {e}")
        return

    yield SSE_DONE


async def stream_groq(
    key: str, model: str, system: str, user_msg: str,
    images: list[dict] = None, history: list[dict] = None
) -> AsyncGenerator[dict, None]:
    """Groq — OpenAI-compatible, free tier available."""
    async for chunk in stream_openai(
        key, model, system, user_msg, images, history,
        api_url="https://api.groq.com/openai/v1/chat/completions",
    ):
        yield chunk


async def stream_deepseek(
    key: str, model: str, system: str, user_msg: str,
    images: list[dict] = None, history: list[dict] = None
) -> AsyncGenerator[dict, None]:
    """DeepSeek — OpenAI-compatible, very cheap."""
    async for chunk in stream_openai(
        key, model, system, user_msg, images, history,
        api_url="https://api.deepseek.com/v1/chat/completions",
    ):
        yield chunk


async def stream_mistral(
    key: str, model: str, system: str, user_msg: str,
    images: list[dict] = None, history: list[dict] = None
) -> AsyncGenerator[dict, None]:
    """Mistral AI — OpenAI-compatible, free tier available."""
    async for chunk in stream_openai(
        key, model, system, user_msg, images, history,
        api_url="https://api.mistral.ai/v1/chat/completions",
    ):
        yield chunk


async def stream_together(
    key: str, model: str, system: str, user_msg: str,
    images: list[dict] = None, history: list[dict] = None
) -> AsyncGenerator[dict, None]:
    """Together.ai — OpenAI-compatible, many open-source models."""
    async for chunk in stream_openai(
        key, model, system, user_msg, images, history,
        api_url="https://api.together.xyz/v1/chat/completions",
    ):
        yield chunk


async def stream_ollama(
    base_url: str, model: str, system: str, user_msg: str,
    images: list[dict] = None, history: list[dict] = None
) -> AsyncGenerator[dict, None]:
    """Ollama — local inference, completely free, no key required."""
    url = (base_url or "http://localhost:11434").rstrip("/")
    async for chunk in stream_openai(
        "ollama",                                  # key ignored by Ollama
        model, system, user_msg, images, history,
        api_url=f"{url}/v1/chat/completions",
    ):
        yield chunk


async def stream_claude_web(
    session_key: str, model: str, system: str, user_msg: str,
    images: list[dict] = None, history: list[dict] = None,
) -> AsyncGenerator[dict, None]:
    """Claude.ai web — uses your Claude Pro/Team subscription session cookie.
    No API key needed. Session key = the 'sessionKey' cookie from claude.ai.
    NOTE: Unofficial. May break if Anthropic changes their web API.
    """
    images  = images  or []
    history = history or []

    if not session_key or len(session_key) < 20:
        yield _sse_error("Claude.ai session key not set — go to Settings → AI Providers → Claude.ai Web")
        return

    cache_key = session_key[:20]
    headers = {
        "User-Agent":                   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept":                       "application/json, text/plain, */*",
        "Accept-Language":              "en-US,en;q=0.9",
        "Origin":                       "https://claude.ai",
        "Referer":                      "https://claude.ai/",
        "anthropic-client-platform":    "web_claude_ai",
    }
    # Clerk JWT (from bookmarklet) starts with "eyJ" — use as Bearer token
    # Traditional sessionKey cookie starts with "sk-ant-sid" — use as Cookie
    if session_key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {session_key}"
    else:
        headers["Cookie"] = f"sessionKey={session_key}"

    try:
        async with httpx.AsyncClient(verify=OUTBOUND_TLS_VERIFY, timeout=TIMEOUT, follow_redirects=True) as client:

            # Step 1: Resolve org UUID (cached)
            org_id = _claude_web_org_cache.get(cache_key)
            if not org_id:
                r = await client.get("https://claude.ai/api/organizations", headers=headers)
                if r.status_code in (401, 403):
                    yield _sse_error("Claude.ai session expired — refresh your sessionKey in Settings → AI Providers")
                    return
                if r.status_code != 200:
                    yield _sse_error(f"Claude.ai: cannot get org (HTTP {r.status_code}) — check sessionKey")
                    return
                orgs = r.json()
                if not orgs:
                    yield _sse_error("Claude.ai: no organization found. Make sure you're logged in.")
                    return
                org_id = orgs[0].get("uuid") or orgs[0].get("id")
                _claude_web_org_cache[cache_key] = org_id

            if not org_id:
                yield _sse_error("Claude.ai: could not determine organization UUID")
                return

            # Step 2: Create a new conversation
            conv_id      = str(_uuid_lib.uuid4())
            conv_payload = {"uuid": conv_id, "name": "NOC Sentinel"}
            if model and model.startswith("claude-"):
                conv_payload["model"] = model

            cr = await client.post(
                f"https://claude.ai/api/organizations/{org_id}/chat_conversations",
                headers={**headers, "Content-Type": "application/json"},
                json=conv_payload,
            )
            if cr.status_code in (401, 403):
                _claude_web_org_cache.pop(cache_key, None)
                yield _sse_error("Claude.ai session expired — please refresh your sessionKey")
                return
            if cr.status_code not in (200, 201):
                yield _sse_error(f"Claude.ai: failed to create conversation (HTTP {cr.status_code})")
                return

            # Step 3: Build prompt (system + history + current message)
            parts = []
            if system:
                parts.append(f"[System]\n{system}")
            for m in (history or [])[-8:]:
                role = "Human" if m["role"] == "user" else "Assistant"
                parts.append(f"{role}: {m['content']}")
            full_prompt = ("\n\n".join(parts) + f"\n\nHuman: {user_msg}") if parts else user_msg

            # Step 4: Stream completion
            async with client.stream(
                "POST",
                f"https://claude.ai/api/organizations/{org_id}/chat_conversations/{conv_id}/completion",
                headers={**headers, "Accept": "text/event-stream", "Content-Type": "application/json"},
                json={
                    "prompt":         full_prompt,
                    "timezone":       "UTC",
                    "attachments":    [],
                    "files":          [],
                    "rendering_mode": "raw",
                },
            ) as resp:
                if resp.status_code in (401, 403):
                    _claude_web_org_cache.pop(cache_key, None)
                    yield _sse_error("Claude.ai session expired — please refresh your sessionKey")
                    return
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield _sse_error(f"Claude.ai stream error {resp.status_code}: {body.decode()[:200]}")
                    return

                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                    except Exception:
                        continue
                    ev_type = ev.get("type", "")
                    if ev_type == "content_block_delta":
                        text = ev.get("delta", {}).get("text", "")
                        if text:
                            yield _sse_text(text)
                    elif ev_type == "completion":          # older claude.ai format
                        text = ev.get("completion", "")
                        if text:
                            yield _sse_text(text)
                    elif ev_type == "message_stop":
                        break
                    elif ev_type == "error":
                        msg = ev.get("error", {})
                        yield _sse_error(str(msg.get("message", msg) if isinstance(msg, dict) else msg))
                        return
    except Exception as e:
        yield _sse_error(f"Claude.ai web error: {e}")
        return

    yield SSE_DONE


async def stream_chatgpt_web(
    access_token: str, model: str, system: str, user_msg: str,
    images: list[dict] = None, history: list[dict] = None,
) -> AsyncGenerator[dict, None]:
    """ChatGPT.com web — uses your ChatGPT Plus/Team subscription access token.
    No API key needed. Token = Authorization header value from chatgpt.com network requests.
    NOTE: Unofficial. Cloudflare may block automated requests.
    """
    images  = images  or []
    history = history or []
    bundle: dict = {}
    token_value = str(access_token or "").strip()
    if token_value.startswith("{"):
        try:
            parsed = json.loads(token_value)
            if isinstance(parsed, dict):
                bundle = parsed
                token_value = str(parsed.get("access_token") or parsed.get("token") or "").strip()
        except Exception:
            bundle = {}
    access_token = token_value

    if not access_token or len(access_token) < 20:
        yield _sse_error("ChatGPT web token not set — go to Settings → AI Providers → ChatGPT Web")
        return

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type":  "application/json",
        "Accept":        "text/event-stream",
        "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Origin":        "https://chatgpt.com",
        "Referer":       "https://chatgpt.com/",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    device_id = str(bundle.get("device_id") or "").strip()
    cookies = str(bundle.get("cookies") or "").strip()
    if device_id:
        headers["oai-device-id"] = device_id
    if cookies:
        headers["Cookie"] = cookies

    messages = []
    for m in (history or [])[-8:]:
        messages.append({
            "id":      str(_uuid_lib.uuid4()),
            "author":  {"role": m["role"]},
            "content": {"content_type": "text", "parts": [m["content"]]},
            "metadata": {},
        })
    messages.append({
        "id":      str(_uuid_lib.uuid4()),
        "author":  {"role": "user"},
        "content": {"content_type": "text", "parts": [user_msg]},
        "metadata": {},
    })

    payload: dict = {
        "action":                       "next",
        "messages":                     messages,
        "model":                        model or "gpt-4o",
        "parent_message_id":            str(_uuid_lib.uuid4()),
        "timezone_offset_min":          0,
        "history_and_training_disabled": True,
        "conversation_mode":            {"kind": "primary_assistant"},
    }
    if system:
        payload["system_prompt"] = system

    try:
        async with httpx.AsyncClient(verify=OUTBOUND_TLS_VERIFY, timeout=TIMEOUT, follow_redirects=True) as client:
            async with client.stream(
                "POST",
                "https://chatgpt.com/backend-api/conversation",
                headers=headers,
                json=payload,
            ) as resp:
                if resp.status_code == 401:
                    yield _sse_error("ChatGPT session expired — please refresh your access token in Settings")
                    return
                if resp.status_code == 403:
                    body = await resp.aread()
                    snippet = body.decode(errors="ignore")[:200]
                    if "<html" in snippet.lower():
                        yield _sse_error(
                            "ChatGPT web is blocking this server session (Cloudflare / browser verification). "
                            "Recapture the web session from Settings so NOC Sentinel stores token + browser bundle."
                        )
                    else:
                        yield _sse_error(f"ChatGPT web blocked the request: {snippet}")
                    return
                if resp.status_code == 429:
                    yield _sse_error("ChatGPT rate limit reached — please wait a moment and try again")
                    return
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield _sse_error(f"ChatGPT web error {resp.status_code}: {body.decode()[:200]}")
                    return

                last_text = ""
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        ev = json.loads(raw)
                    except Exception:
                        continue
                    msg = ev.get("message", {})
                    if not msg or msg.get("author", {}).get("role") != "assistant":
                        continue
                    content = msg.get("content", {})
                    if content.get("content_type") != "text":
                        continue
                    parts = content.get("parts", [])
                    if not parts or not isinstance(parts[0], str):
                        continue
                    current_text = parts[0]
                    if len(current_text) > len(last_text):
                        yield _sse_text(current_text[len(last_text):])
                        last_text = current_text
                    if msg.get("end_turn"):
                        break
    except Exception as e:
        yield _sse_error(f"ChatGPT web error: {e}")
        return

    yield SSE_DONE


async def stream_ai(
    provider: str, key: str, model: str, system: str, user_msg: str,
    images: list[dict] = None, history: list[dict] = None
) -> AsyncGenerator[dict, None]:
    """Route to the correct provider's streaming function.
    For Ollama, `key` should be the base URL (e.g. http://localhost:11434).
    For claude_web, `key` is the claude.ai sessionKey cookie value.
    For chatgpt_web, `key` is the chatgpt.com Bearer access token.
    """
    if provider == "claude":
        gen = stream_claude(key, model, system, user_msg, images, history)
    elif provider == "openai":
        gen = stream_openai(key, model, system, user_msg, images, history)
    elif provider == "grok":
        gen = stream_grok(key, model, system, user_msg, images, history)
    elif provider == "gemini":
        gen = stream_gemini(key, model, system, user_msg, images, history)
    elif provider == "openrouter":
        gen = stream_openrouter(key, model, system, user_msg, images, history)
    elif provider == "groq":
        gen = stream_groq(key, model, system, user_msg, images, history)
    elif provider == "deepseek":
        gen = stream_deepseek(key, model, system, user_msg, images, history)
    elif provider == "mistral":
        gen = stream_mistral(key, model, system, user_msg, images, history)
    elif provider == "together":
        gen = stream_together(key, model, system, user_msg, images, history)
    elif provider == "ollama":
        gen = stream_ollama(key, model, system, user_msg, images, history)
    elif provider == "claude_web":
        gen = stream_claude_web(key, model, system, user_msg, images, history)
    elif provider == "chatgpt_web":
        gen = stream_chatgpt_web(key, model, system, user_msg, images, history)
    else:
        yield _sse_error(f"Unknown provider: {provider}")
        return

    async for chunk in gen:
        yield chunk
