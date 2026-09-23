"""Application configuration.

All configuration comes from environment variables prefixed with ``CAREOS_``.
Secrets are never hard-coded; development defaults are rejected in production.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


_INSECURE_MARKERS = ("insecure", "change-me", "replace-with", "dev-only")


def _alias(*names: str) -> AliasChoices:
    """Accept both the CAREOS_-prefixed name and the vendor's conventional variable name."""
    return AliasChoices(*names)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CAREOS_",
        env_file=None,
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    environment: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"
    log_json: bool = True

    # --- persistence -----------------------------------------------------------
    database_url: SecretStr = SecretStr("postgresql+asyncpg://careos:careos@localhost:5432/careos")
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_echo: bool = False
    redis_url: str | None = "redis://localhost:6379/0"
    #: Redis is optional (rate limiting, realtime fan-out). Keep its timeouts short so an
    #: unreachable Redis costs one short wait before the circuit opens, not one per request.
    redis_timeout_seconds: float = Field(default=0.5, gt=0, le=10)
    redis_circuit_reset_seconds: float = Field(default=5.0, gt=0, le=300)
    #: Upper bound on post-commit realtime publishing per message (never blocks safety work).
    realtime_publish_timeout_seconds: float = Field(default=1.0, gt=0, le=10)

    # --- security ----------------------------------------------------------------
    secret_key: SecretStr = SecretStr("dev-insecure-secret-key-do-not-use-in-production")
    session_ttl_minutes: int = Field(default=720, ge=5, le=24 * 60)
    session_idle_timeout_minutes: int = Field(default=120, ge=5, le=24 * 60)
    cookie_secure: bool = False
    cookie_domain: str | None = None
    cors_allowed_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    login_max_attempts: int = Field(default=10, ge=1)
    login_window_seconds: int = Field(default=300, ge=10)
    gateway_max_requests_per_minute: int = Field(default=600, ge=1)

    # --- device gateway / simulator ------------------------------------------------
    simulator_enabled: bool = True
    simulator_gateway_key: SecretStr | None = None
    gateway_internal_url: str = "http://localhost:8000"

    # --- providers -----------------------------------------------------------------
    voice_provider: Literal["mock", "twilio", "disabled"] = "mock"
    notification_provider: Literal["mock", "disabled"] = "mock"
    ai_provider: Literal["mock", "mock_unavailable", "disabled"] = "mock"
    mock_voice_outcome: Literal["no_answer", "answered_acknowledged", "failure"] = "no_answer"
    provider_timeout_seconds: float = Field(default=10.0, gt=0, le=120)

    # --- real telephony (Twilio) ------------------------------------------------------
    #: Master safety switch. Unless explicitly true, TwilioVoiceProvider refuses every call.
    real_telephony_enabled: bool = False
    #: Outside production, real calls may only go to these E.164 numbers (comma separated).
    telephony_allowed_numbers: Annotated[list[str], NoDecode] = []
    twilio_account_sid: str | None = Field(
        default=None, validation_alias=_alias("CAREOS_TWILIO_ACCOUNT_SID", "TWILIO_ACCOUNT_SID")
    )
    twilio_auth_token: SecretStr | None = Field(
        default=None, validation_alias=_alias("CAREOS_TWILIO_AUTH_TOKEN", "TWILIO_AUTH_TOKEN")
    )
    twilio_from_number: str | None = Field(
        default=None, validation_alias=_alias("CAREOS_TWILIO_FROM_NUMBER", "TWILIO_FROM_NUMBER")
    )
    #: UK-first deployment: Twilio's Ireland region keeps call control in the EU.
    twilio_region: str | None = Field(
        default="ie1", validation_alias=_alias("CAREOS_TWILIO_REGION", "TWILIO_REGION")
    )
    twilio_edge: str | None = Field(
        default="dublin", validation_alias=_alias("CAREOS_TWILIO_EDGE", "TWILIO_EDGE")
    )
    twilio_api_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    twilio_ring_timeout_seconds: int = Field(default=30, ge=5, le=120)
    #: Public HTTPS base URL Twilio can reach for status callbacks and the media WebSocket.
    twilio_webhook_base_url: str | None = None
    voice_call_max_duration_seconds: int = Field(default=240, ge=10, le=1800)
    voice_call_poll_interval_seconds: float = Field(default=1.0, gt=0, le=10)
    media_max_connections: int = Field(default=50, ge=1, le=1000)
    media_max_message_bytes: int = Field(default=64_000, ge=1_000, le=1_000_000)

    # --- AI voice ---------------------------------------------------------------------
    ai_voice_provider: Literal["mock", "openai_realtime", "disabled"] = "mock"
    openai_api_key: SecretStr | None = Field(
        default=None, validation_alias=_alias("CAREOS_OPENAI_API_KEY", "OPENAI_API_KEY")
    )
    openai_realtime_model: str = Field(
        default="gpt-realtime",
        validation_alias=_alias("CAREOS_OPENAI_REALTIME_MODEL", "OPENAI_REALTIME_MODEL"),
    )
    ai_connect_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    ai_response_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    #: Spoken first; must always disclose that the caller is automated, never a human.
    voice_greeting_template: str = (
        "Hello {preferred_name}. This is the automated CareOS safety assistant responding "
        "to your alert. I am an automated system, not a human operator. I can help connect "
        "you with your care team."
    )

    # --- worker / escalation ---------------------------------------------------------
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    worker_batch_size: int = Field(default=20, ge=1, le=500)
    worker_lease_seconds: int = Field(default=60, ge=5)
    escalation_max_attempts: int = Field(default=3, ge=1, le=10)
    escalation_retry_base_seconds: float = Field(default=5.0, ge=0)
    #: A step still waiting this long after it was due means the worker is down or saturated:
    #: /ready reports the worker as lagging and consoles tell operators to act manually.
    escalation_overdue_after_seconds: int = Field(default=60, ge=5)
    device_offline_after_seconds: int = Field(default=86_400, ge=60)
    device_low_battery_threshold: int = Field(default=20, ge=1, le=100)
    device_sweep_interval_seconds: float = Field(default=30.0, gt=0)
    #: Development convenience: run the worker loops inside the API process (single process,
    #: no Redis needed for live console updates). Never used in staging/production.
    embedded_worker: bool = False

    # --- demo seed -------------------------------------------------------------------
    seed_demo_password: SecretStr | None = None

    metrics_enabled: bool = True

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator(
        "cookie_domain",
        "redis_url",
        "twilio_account_sid",
        "twilio_from_number",
        "twilio_region",
        "twilio_edge",
        "twilio_webhook_base_url",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        return value or None

    @field_validator("telephony_allowed_numbers", mode="before")
    @classmethod
    def _split_numbers(cls, value: object) -> object:
        if isinstance(value, str):
            return [number.strip() for number in value.split(",") if number.strip()]
        return value

    @model_validator(mode="after")
    def _enforce_production_safety(self) -> Settings:
        secret = self.secret_key.get_secret_value()
        if len(secret) < 32:
            raise ValueError("CAREOS_SECRET_KEY must be at least 32 characters")
        if self.environment in (Environment.STAGING, Environment.PRODUCTION):
            problems: list[str] = []
            if any(marker in secret.lower() for marker in _INSECURE_MARKERS):
                problems.append("CAREOS_SECRET_KEY uses a development placeholder")
            if not self.cookie_secure:
                problems.append("CAREOS_COOKIE_SECURE must be true")
            if self.simulator_enabled:
                problems.append("CAREOS_SIMULATOR_ENABLED must be false")
            if self.embedded_worker:
                problems.append("CAREOS_EMBEDDED_WORKER must be false (run a separate worker)")
            if "*" in self.cors_allowed_origins:
                problems.append("CAREOS_CORS_ALLOWED_ORIGINS must not contain '*'")
            if problems:
                raise ValueError("Unsafe production configuration: " + "; ".join(problems))
        if self.real_telephony_enabled:
            missing = [
                name
                for name, value in (
                    ("TWILIO_ACCOUNT_SID", self.twilio_account_sid),
                    ("TWILIO_AUTH_TOKEN", self.twilio_auth_token),
                    ("TWILIO_FROM_NUMBER", self.twilio_from_number),
                    ("CAREOS_TWILIO_WEBHOOK_BASE_URL", self.twilio_webhook_base_url),
                )
                if not value
            ]
            if missing:
                raise ValueError("CAREOS_REAL_TELEPHONY_ENABLED requires: " + ", ".join(missing))
            if not str(self.twilio_webhook_base_url).startswith("https://"):
                raise ValueError("CAREOS_TWILIO_WEBHOOK_BASE_URL must be a public https:// URL")
            if not self.is_production_like and not self.telephony_allowed_numbers:
                raise ValueError(
                    "Real telephony outside production requires CAREOS_TELEPHONY_ALLOWED_NUMBERS"
                )
        return self

    @property
    def is_production_like(self) -> bool:
        return self.environment in (Environment.STAGING, Environment.PRODUCTION)


@lru_cache
def get_settings() -> Settings:
    return Settings()
