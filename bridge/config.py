from __future__ import annotations

from collections import Counter
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ReleaseMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"
    APPROVAL = "approval"


MediaType = Literal["movie", "show"]


class BridgeProfile(BaseModel):
    id: int
    name: str
    media_types: list[MediaType] = Field(default_factory=lambda: ["movie", "show"])
    mode: ReleaseMode = ReleaseMode.AUTO
    mediamanager_library: str = "Default"
    mediamanager_ruleset: str = "default"
    score_set: str = "default"
    max_results: int = Field(default=10, ge=1, le=100)
    trash_profile_id: str | None = None
    trash_profile_url: HttpUrl | None = None
    trash_custom_format_group_ids: list[str] = Field(default_factory=list)
    profilarr_profile_id: str | None = None


class RootFolder(BaseModel):
    id: int
    path: str
    free_space: int = 0


class Tag(BaseModel):
    id: int
    label: str


class BridgeConfig(BaseModel):
    profiles: list[BridgeProfile] = Field(
        default_factory=lambda: [
            BridgeProfile(id=101, name="TRaSH: HD Bluray + WEB 1080p")
        ]
    )
    root_folders: list[RootFolder] = Field(
        default_factory=lambda: [RootFolder(id=1, path="Default")]
    )
    tags: list[Tag] = Field(default_factory=lambda: [Tag(id=1, label="onscreen")])

    @model_validator(mode="after")
    def validate_profile_ids(self) -> Self:
        counts = Counter(profile.id for profile in self.profiles)
        duplicate_ids = sorted(profile_id for profile_id, count in counts.items() if count > 1)
        if duplicate_ids:
            joined = ", ".join(str(profile_id) for profile_id in duplicate_ids)
            raise ValueError(f"profile ids must be unique: {joined}")
        return self

    def profile_for(self, profile_id: int | None, media_type: MediaType) -> BridgeProfile:
        if profile_id is not None:
            for profile in self.profiles:
                if profile.id == profile_id:
                    if media_type not in profile.media_types:
                        raise ValueError(f"profile {profile_id} does not support {media_type}")
                    return profile
            raise ValueError(f"profile {profile_id} does not exist")
        candidates = [p for p in self.profiles if media_type in p.media_types]
        if not candidates:
            raise ValueError(f"no bridge profile supports {media_type}")
        return candidates[0]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bridge_api_key: SecretStr = Field(default="", alias="BRIDGE_API_KEY")
    mediamanager_base_url: HttpUrl | str = Field(
        default="http://mediamanager:8000", alias="MEDIAMANAGER_BASE_URL"
    )
    mediamanager_username: SecretStr = Field(default="", alias="MEDIAMANAGER_USERNAME")
    mediamanager_password: SecretStr = Field(default="", alias="MEDIAMANAGER_PASSWORD")
    mediamanager_token: SecretStr = Field(default="", alias="MEDIAMANAGER_TOKEN")
    mediamanager_timeout_seconds: float = Field(
        default=120.0,
        alias="MEDIAMANAGER_TIMEOUT_SECONDS",
        gt=0,
        le=600,
    )
    config_path: Path = Field(default=Path("/config/config.yaml"), alias="CONFIG_PATH")
    log_level: str = Field(default="info", alias="LOG_LEVEL")
    auto_download_full_series: bool = Field(default=False, alias="AUTO_DOWNLOAD_FULL_SERIES")
    max_auto_tv_seasons: int = Field(default=3, alias="MAX_AUTO_TV_SEASONS")
    enable_dashboard: bool = Field(default=False, alias="ENABLE_DASHBOARD")
    screenarr_data_path: Path = Field(
        default=Path("/data/screenarr.db"),
        alias="SCREENARR_DATA_PATH",
    )
    dashboard_session_secret: SecretStr = Field(default="", alias="DASHBOARD_SESSION_SECRET")
    dashboard_session_ttl_minutes: int = Field(
        default=720,
        alias="DASHBOARD_SESSION_TTL_MINUTES",
        ge=1,
        le=10080,
    )
    onscreen_webhook_secret: SecretStr = Field(default="", alias="ONSCREEN_WEBHOOK_SECRET")
    enable_onscreen_webhook: bool = Field(default=False, alias="ENABLE_ONSCREEN_WEBHOOK")
    enable_mediamanager_reconcile: bool = Field(
        default=False,
        alias="ENABLE_MEDIAMANAGER_RECONCILE",
    )
    mediamanager_reconcile_interval_seconds: int = Field(
        default=300,
        alias="MEDIAMANAGER_RECONCILE_INTERVAL_SECONDS",
        ge=30,
    )
    mediamanager_config_path: Path | None = Field(default=None, alias="MEDIAMANAGER_CONFIG_PATH")

    @field_validator("mediamanager_config_path", mode="before")
    @classmethod
    def blank_path_is_disabled(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @model_validator(mode="after")
    def validate_webhook_secret(self) -> Self:
        dashboard_secret = self.dashboard_session_secret.get_secret_value().strip()
        if dashboard_secret and len(dashboard_secret) < 32:
            raise ValueError("DASHBOARD_SESSION_SECRET must be at least 32 characters")
        self.dashboard_session_secret = SecretStr(dashboard_secret)
        secret_value = self.onscreen_webhook_secret.get_secret_value().strip()
        if self.enable_onscreen_webhook and len(secret_value) < 32:
            raise ValueError(
                "ONSCREEN_WEBHOOK_SECRET must be set to at least 32 characters "
                "when ENABLE_ONSCREEN_WEBHOOK is true"
            )
        # Normalize configured secrets on load so later HMAC checks use the validated value.
        self.onscreen_webhook_secret = SecretStr(secret_value)
        return self


def load_bridge_config(path: Path) -> BridgeConfig:
    if not path.exists():
        return BridgeConfig()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in bridge config {path}: {exc}") from exc
    return BridgeConfig.model_validate(raw)
