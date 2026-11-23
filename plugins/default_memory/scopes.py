"""Apply the imported learning and recall policies to immutable session scopes."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from plugins.ledger.contract import MessageCatalog


class ScopePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    learn: Literal["global", "isolated", "off"]
    recall: bool = True


@dataclass(frozen=True, slots=True)
class Route:
    write: str | None
    read: str | None


class MemoryScopes(Protocol):
    def route(self, session_id: str) -> Route: ...


class ScopeRoutes:
    def __init__(self, catalog: MessageCatalog, policies: Mapping[str, ScopePolicy]):
        self._catalog = catalog
        self._policies = dict(policies)

    def route(self, session_id: str) -> Route:
        attributes = self._catalog.attributes(session_id)
        matches = [(pair, self._policies.get("=".join(pair))) for pair in attributes.scope]
        isolated = sorted(pair for pair, policy in matches if policy is not None and policy.learn == "isolated")
        graph = "v2:" + json.dumps(isolated, ensure_ascii=False, separators=(",", ":")) if isolated else "default"
        disabled = tuple(policy for _, policy in matches if policy is not None)
        return Route(
            write=None if attributes.learning == "excluded" or any(p.learn == "off" for p in disabled) else graph,
            read=None if any(not p.recall for p in disabled) else graph,
        )
