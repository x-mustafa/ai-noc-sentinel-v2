"""Shared AI provider/model/credential resolution for all AI entrypoints."""

from __future__ import annotations

from typing import Any


PROVIDER_KEY_FIELDS: dict[str, str] = {
    "claude_code": "",           # no key needed — uses local CLI auth
    "claude": "claude_key",
    "openai": "openai_key",
    "gemini": "gemini_key",
    "grok": "grok_key",
    "openrouter": "openrouter_key",
    "groq": "groq_key",
    "deepseek": "deepseek_key",
    "mistral": "mistral_key",
    "together": "together_key",
    "ollama": "ollama_url",
    "claude_web": "claude_web_session",
    "chatgpt_web": "chatgpt_web_token",
}

# Providers that work without any credential configured
NO_KEY_PROVIDERS: frozenset[str] = frozenset({"ollama", "claude_code"})


MODEL_DEFAULTS: dict[str, str] = {
    "claude_code": "claude-sonnet-4-6",
    "claude": "claude-sonnet-4-6",
    "openai": "gpt-4o",
    "gemini": "gemini-2.0-flash",
    "grok": "grok-2-latest",
    "openrouter": "anthropic/claude-3.5-haiku",
    "groq": "llama-3.3-70b-versatile",
    "deepseek": "deepseek-chat",
    "mistral": "mistral-small-latest",
    "together": "meta-llama/Llama-3-70b-chat-hf",
    "ollama": "llama3.2",
    "claude_web": "claude-3-5-sonnet-20241022",
    "chatgpt_web": "gpt-4o",
}

FAILOVER_ORDER: tuple[str, ...] = (
    "claude_code",
    "chatgpt_web",
    "claude_web",
    "openai",
    "claude",
    "gemini",
    "grok",
    "openrouter",
    "groq",
    "deepseek",
    "mistral",
    "together",
    "ollama",
)


def normalize_provider(provider: str | None) -> str:
    value = str(provider or "").strip().lower()
    return value if value in PROVIDER_KEY_FIELDS else "claude_code"


def provider_credential_field(provider: str | None) -> str:
    return PROVIDER_KEY_FIELDS.get(normalize_provider(provider), "claude_key")


def provider_credential(cfg: dict[str, Any] | None, provider: str | None) -> str:
    if not cfg:
        return ""
    field = provider_credential_field(provider)
    value = cfg.get(field)
    return str(value or "").strip()


def provider_default_model(provider: str | None, fallback: str = "claude") -> str:
    normalized = normalize_provider(provider)
    if normalized in MODEL_DEFAULTS:
        return MODEL_DEFAULTS[normalized]
    return MODEL_DEFAULTS.get(normalize_provider(fallback), MODEL_DEFAULTS["claude"])


def resolve_runtime_ai(
    cfg: dict[str, Any] | None,
    provider: str | None = None,
    model: str | None = None,
    *,
    fallback_provider: str = "claude",
    allow_fallback: bool = True,
) -> tuple[str, str, str]:
    """Return (provider, model, credential_or_endpoint)."""
    config = cfg or {}
    default_provider = normalize_provider(config.get("default_ai_provider") or fallback_provider)
    selected_provider = normalize_provider(provider or config.get("default_ai_provider") or fallback_provider)
    selected_model = str(model or "").strip()
    if not selected_model:
        configured_default_model = str(config.get("default_ai_model") or "").strip()
        if configured_default_model and (not provider or selected_provider == default_provider):
            selected_model = configured_default_model
    if not selected_model:
        selected_model = provider_default_model(selected_provider, fallback_provider)
    selected_credential = provider_credential(config, selected_provider)

    if not selected_credential and selected_provider not in NO_KEY_PROVIDERS and allow_fallback:
        fallback = normalize_provider(fallback_provider)
        if fallback != selected_provider:
            fallback_credential = provider_credential(config, fallback)
            if fallback_credential or fallback == "ollama":
                selected_provider = fallback
                selected_model = provider_default_model(fallback)
                selected_credential = fallback_credential

    if not selected_model:
        selected_model = provider_default_model(selected_provider, fallback_provider)

    return selected_provider, selected_model, selected_credential


def provider_candidates(
    cfg: dict[str, Any] | None,
    provider: str | None = None,
    model: str | None = None,
    *,
    fallback_provider: str = "claude",
) -> list[tuple[str, str, str]]:
    """Return ordered runtime AI candidates, first preferred then configured fallbacks."""
    config = cfg or {}
    candidates: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def add_candidate(provider_name: str | None, model_name: str | None = None) -> None:
        selected_provider, selected_model, selected_credential = resolve_runtime_ai(
            config,
            provider_name,
            model_name,
            fallback_provider=fallback_provider,
            allow_fallback=False,
        )
        if selected_provider in seen:
            return
        if selected_provider not in NO_KEY_PROVIDERS and not str(selected_credential or "").strip():
            return
        seen.add(selected_provider)
        candidates.append((selected_provider, selected_model, selected_credential))

    preferred_provider = normalize_provider(provider or config.get("default_ai_provider") or fallback_provider)
    preferred_model = str(
        model
        or config.get("default_ai_model")
        or provider_default_model(preferred_provider, fallback_provider)
    ).strip()
    add_candidate(preferred_provider, preferred_model)

    default_provider = normalize_provider(config.get("default_ai_provider") or fallback_provider)
    if default_provider != preferred_provider:
        add_candidate(default_provider, str(config.get("default_ai_model") or "").strip() or None)

    for candidate in FAILOVER_ORDER:
        if candidate in seen:
            continue
        model_name = str(config.get("default_ai_model") or "").strip() if candidate == default_provider else None
        add_candidate(candidate, model_name or None)

    return candidates
