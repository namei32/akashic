"""Public, I/O-free management request schemas."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from .schema import Kind


class Browse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    query: str = Field(default="", max_length=2000)
    kind: Kind | None = None
    status: Literal["active", "superseded", "all"] = "active"
    source_ref: str = Field(default="", max_length=2000)
    has_embedding: bool | None = None
    sort_by: Literal["updated_at", "created_at", "happened_at", "reinforcement", "emotional_weight", "kind"] = "updated_at"
    sort_order: Literal["asc", "desc"] = "desc"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)



class SourceTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)


class SourceRetraction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1)
    user_message_id: str = Field(min_length=1)
    sources: tuple[SourceTarget, ...] = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=500)


class Correction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1)
    user_message_id: str = Field(min_length=1)
    memory_id: str = Field(min_length=1)
    expected_fact_version: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=500)
    happened_at: str | None = None
    emotional_weight: float | None = Field(default=None, ge=0, le=10)
    category: str | None = Field(default=None, max_length=80)
    steps: tuple[str, ...] | None = None
    tool_requirement: str | None = None


class Merge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1)
    user_message_id: str = Field(min_length=1)
    memory_ids: tuple[str, ...] = Field(min_length=2, max_length=10)
    expected_fact_versions: dict[str, str]
    reason: str = Field(min_length=1, max_length=500)


class Similar(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    memory_id: str
    limit: int = Field(default=8, ge=1, le=50)
    include_superseded: bool = False
