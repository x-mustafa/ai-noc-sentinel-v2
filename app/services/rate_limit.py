from collections import defaultdict
import logging
import time

from fastapi import HTTPException

from app.config import settings

logger = logging.getLogger(__name__)

_memory_attempts: dict[str, list[float]] = defaultdict(list)
_redis_client = None
_redis_warning_logged = False


async def _get_redis_client():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    if not settings.redis_url:
        _log_redis_fallback("REDIS_URL is not configured for redis rate limiting.")
        return None
    try:
        from redis.asyncio import from_url

        _redis_client = from_url(settings.redis_url, decode_responses=True)
        return _redis_client
    except Exception as exc:
        _log_redis_fallback(f"Redis unavailable: {exc}")
        return None


def _log_redis_fallback(message: str) -> None:
    global _redis_warning_logged
    if _redis_warning_logged:
        return
    logger.warning("%s Falling back to in-process rate limiting.", message)
    _redis_warning_logged = True


def _memory_key(identity: str) -> str:
    return identity or "unknown"


def _normalize_identities(identities: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(identities, str):
        raw = [identities]
    else:
        raw = list(identities or [])
    normalized = []
    seen = set()
    for item in raw:
        key = _memory_key(str(item).strip())
        if key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized or ["unknown"]


def _get_memory_attempts(identity: str, window_seconds: int) -> list[float]:
    now = time.time()
    key = _memory_key(identity)
    recent = [ts for ts in _memory_attempts[key] if now - ts < window_seconds]
    _memory_attempts[key] = recent
    return recent


def _assert_memory_limit(identity: str, window_seconds: int, max_attempts: int) -> None:
    recent = _get_memory_attempts(identity, window_seconds)
    if len(recent) >= max_attempts:
        raise HTTPException(429, "Too many login attempts. Try again later.")


def _record_memory_failure(identity: str, window_seconds: int, max_attempts: int) -> None:
    recent = _get_memory_attempts(identity, window_seconds)
    recent.append(time.time())
    _memory_attempts[_memory_key(identity)] = recent
    if len(recent) >= max_attempts:
        raise HTTPException(429, "Too many login attempts. Try again later.")


def _reset_memory_limit(identity: str) -> None:
    _memory_attempts.pop(_memory_key(identity), None)


async def _assert_redis_limit(identity: str, window_seconds: int, max_attempts: int) -> bool:
    client = await _get_redis_client()
    if client is None:
        return False
    key = f"login-rate:{identity}"
    try:
        raw_attempts = await client.get(key)
        attempts = int(raw_attempts or 0)
        if attempts >= max_attempts:
            ttl = await client.ttl(key)
            wait_for = ttl if isinstance(ttl, int) and ttl > 0 else window_seconds
            raise HTTPException(429, f"Too many login attempts. Try again in {wait_for} seconds.")
        return True
    except HTTPException:
        raise
    except Exception as exc:
        _log_redis_fallback(f"Redis rate limit error: {exc}")
        return False


async def _record_redis_failure(identity: str, window_seconds: int, max_attempts: int) -> bool:
    client = await _get_redis_client()
    if client is None:
        return False
    key = f"login-rate:{identity}"
    try:
        attempts = await client.incr(key)
        if attempts == 1:
            await client.expire(key, window_seconds)
        if attempts >= max_attempts:
            ttl = await client.ttl(key)
            wait_for = ttl if isinstance(ttl, int) and ttl > 0 else window_seconds
            raise HTTPException(429, f"Too many login attempts. Try again in {wait_for} seconds.")
        return True
    except HTTPException:
        raise
    except Exception as exc:
        _log_redis_fallback(f"Redis rate limit error: {exc}")
        return False


async def assert_login_rate_limit(identities: str | list[str] | tuple[str, ...], window_seconds: int, max_attempts: int) -> None:
    mode = settings.login_rate_limit_mode
    if mode == "proxy":
        return

    keys = _normalize_identities(identities)

    if mode == "redis":
        redis_ok = True
        for key in keys:
            if not await _assert_redis_limit(key, window_seconds, max_attempts):
                redis_ok = False
                break
        if redis_ok:
            return

    for key in keys:
        _assert_memory_limit(key, window_seconds, max_attempts)


async def record_login_failure(identities: str | list[str] | tuple[str, ...], window_seconds: int, max_attempts: int) -> None:
    mode = settings.login_rate_limit_mode
    if mode == "proxy":
        return

    keys = _normalize_identities(identities)

    if mode == "redis":
        redis_ok = True
        for key in keys:
            if not await _record_redis_failure(key, window_seconds, max_attempts):
                redis_ok = False
                break
        if redis_ok:
            return

    for key in keys:
        _record_memory_failure(key, window_seconds, max_attempts)


async def check_login_rate_limit(identity: str, window_seconds: int, max_attempts: int) -> None:
    await assert_login_rate_limit(identity, window_seconds, max_attempts)


async def reset_login_rate_limit(identities: str | list[str] | tuple[str, ...]) -> None:
    if settings.login_rate_limit_mode == "proxy":
        return

    keys = _normalize_identities(identities)

    if settings.login_rate_limit_mode == "redis":
        client = await _get_redis_client()
        if client is not None:
            try:
                await client.delete(*[f"login-rate:{key}" for key in keys])
                return
            except Exception as exc:
                _log_redis_fallback(f"Redis rate limit reset error: {exc}")

    for key in keys:
        _reset_memory_limit(key)


async def close_rate_limiter() -> None:
    global _redis_client
    if _redis_client is None:
        return
    try:
        await _redis_client.aclose()
    except Exception:
        pass
    _redis_client = None
