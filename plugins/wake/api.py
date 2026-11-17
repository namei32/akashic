from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from agent.plugin_composition import EmitEventKey
from .active_sources import SourcePolicy
from plugins.eventmail.contract import EVENTMAIL_DELIVERY_V2 as EVENTMAIL_DELIVERY, EVENTMAIL_WAKE_V2 as EVENTMAIL_WAKE, ContentWakeServicesV2 as ContentWakeServices, EventMailDeliveryServicesV2 as DeliveryServices
from plugins.drift.contract import DRIFT_DELIVERY_V2 as DRIFT_DELIVERY, DRIFT_WAKE_V3 as DRIFT_WAKE, DriftWakeServicesV3 as DriftWakeServices


class DeliveryTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str
    recipient: str
    session_id: str

    @field_validator("channel", "recipient", "session_id")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("Wake delivery target 必须非空且无首尾空白")
        return value


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = True
    delivery: DeliveryTarget | None = None
    timezone: str = "Asia/Shanghai"
    memory_provider: Literal["default_memory", "akasha"] = "default_memory"
    active_sources: SourcePolicy = SourcePolicy()
    investigation_tools: tuple[str, ...] = ("recall_memory", "web_fetch")

    @model_validator(mode="after")
    def active_source_channel(self):
        if self.active_sources.enabled and (self.delivery is None or self.delivery.channel != "akashic"):
            raise ValueError("自动会话来源首版仅支持原生私人 Web 目标")
        return self

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("Wake timezone 必须非空且无首尾空白")
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"Wake timezone 无效: {value}") from error
        return value


EVENTMAIL_CHANGED = EmitEventKey[None]("eventmail.changed")
