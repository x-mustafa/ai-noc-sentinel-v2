from pydantic_settings import BaseSettings
from pydantic import validator


class Settings(BaseSettings):
    db_host: str = "localhost"
    db_port: int = 3306
    db_user: str = "root"
    db_pass: str = ""
    db_name: str = "tabadul_noc"
    app_secret: str = "change_this_to_a_random_string_at_least_32_chars_long"
    session_max_age: int = 86400 * 7  # 7 days
    session_https_only: bool = False
    outbound_tls_verify: bool = False
    db_auto_migrate_on_startup: bool = False
    embedded_scheduler_enabled: bool = False
    redis_url: str = ""
    login_rate_limit_mode: str = "memory"
    password_min_length: int = 12
    login_window_seconds: int = 900
    login_max_attempts: int = 10

    # Microsoft 365 — Graph API
    ms365_email:         str = ""
    ms365_tenant_id:     str = ""
    ms365_client_id:     str = ""
    ms365_client_secret: str = ""

    @validator("app_secret")
    def secret_must_be_strong(cls, v):
        _default = "change_this_to_a_random_string_at_least_32_chars_long"
        if v == _default:
            import logging
            logging.getLogger(__name__).warning(
                "APP_SECRET is using the default placeholder value. "
                "Set a strong random secret in .env before deploying to production. "
                "Generate one with: python3 -c \"import secrets; print(secrets.token_hex(32))\""
            )
        elif len(v) < 32:
            raise ValueError("APP_SECRET must be at least 32 characters long")
        return v

    @validator("password_min_length")
    def password_length_must_be_positive(cls, v):
        if v < 8:
            raise ValueError("PASSWORD_MIN_LENGTH must be at least 8")
        return v

    @validator("login_window_seconds", "login_max_attempts")
    def security_limits_must_be_positive(cls, v):
        if v <= 0:
            raise ValueError("Security rate-limit settings must be positive")
        return v

    @validator("login_rate_limit_mode")
    def login_rate_limit_mode_valid(cls, v):
        allowed = {"memory", "redis", "proxy"}
        if v not in allowed:
            raise ValueError(f"LOGIN_RATE_LIMIT_MODE must be one of: {', '.join(sorted(allowed))}")
        return v

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
