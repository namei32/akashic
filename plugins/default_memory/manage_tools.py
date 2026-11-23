"""User-directed inspection, correction and merge through durable public operations."""
from __future__ import annotations

import json
from collections.abc import Mapping
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from plugins.ledger.contract import ContentPart, Input, json_value, is_user_input
from plugins.tools.contract import CallSource, Result
from .api import Correction, Merge, SourceRetraction, SourceTarget
from .io import run_io
from .runtime import Runtime
from .store import digest


class InspectArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_id: str = Field(min_length=1)


class CorrectArgs(InspectArgs):
    summary: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=500)
    happened_at: str | None = None
    emotional_weight: float | None = Field(default=None, ge=0, le=10)
    category: str | None = Field(default=None, max_length=80)
    steps: tuple[str, ...] | None = None
    tool_requirement: str | None = None


class MergeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_ids: tuple[str, ...] = Field(min_length=2, max_length=10)
    reason: str = Field(min_length=1, max_length=500)


class RetractSourcesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: tuple[SourceTarget, ...] = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=500)


class ManageTool:
    idempotent = True

    def __init__(self, runtime: Runtime, action: str):
        if action not in {"inspect", "correct", "merge", "retract_sources"}:
            raise ValueError("Unknown memory management operation")
        self.runtime, self.action = runtime, action

    async def prepare(self, arguments: Mapping[str, object], source: CallSource | None = None) -> Mapping[str, object] | str:
        try:
            schema = {"inspect": InspectArgs, "correct": CorrectArgs, "merge": MergeArgs,
                      "retract_sources": RetractSourcesArgs}[self.action]
            parsed = schema.model_validate(json_value(arguments))
            if source is None or not source.messages:
                return "记忆管理需要真实对话出处"
            session = source.messages[0].session_id
            graph = self.runtime.route(session, write=self.action != "inspect")
            users = [m for m in source.messages if isinstance(m.body, Input) and is_user_input(m)]
            if self.action != "inspect" and (not users or self.runtime.content.legacy_post_commit_effect(users[-1]) == "suppress"):
                return "记忆修改需要合资格的真实用户输入"
            values = parsed.model_dump(mode="json")
            versions = {}
            if self.action in {"correct", "merge"}:
                ids = values["memory_ids"] if self.action == "merge" else [values["memory_id"]]
                for identity in ids:
                    versions[identity] = (await self.runtime.inspect(session, identity))["fact_version"]
            return {"action": self.action, "session_id": session, "graph": graph,
                    "user_message_id": users[-1].message_id if users else "",
                    "arguments": values, "expected_versions": versions, "effect_key": source.effect_key}
        except (ValidationError, PermissionError, ValueError) as error:
            return str(error)

    @staticmethod
    def result(value: Mapping[str, object]) -> Result:
        return Result("success", (ContentPart("text", str(value["text"])),))

    async def query(self, key: str) -> Result | None:
        value = await run_io(self.runtime.store.receipt, "tool:" + key)
        if value is None:
            return None
        if self.action == "inspect" and self.runtime.route(str(value["session_id"])) != value["graph"]:
            raise PermissionError("记忆范围已经改变")
        return self.result(value)

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> Result:
        data = json_value(arguments)
        if not isinstance(data, dict) or data.get("effect_key") != key or data.get("action") != self.action:
            raise ValueError("Memory management arguments belong to another call")
        session = str(data["session_id"])
        graph = self.runtime.route(session, write=self.action != "inspect")
        if graph != data["graph"]:
            raise PermissionError("Memory scope changed after preparation")
        identity, signature = "tool:" + key, digest(data)
        previous = await run_io(self.runtime.store.receipt, identity, signature)
        if previous is not None:
            return self.result(previous)
        if self.action == "inspect":
            args = InspectArgs.model_validate(data["arguments"])
            detail = await self.runtime.inspect(session, args.memory_id)
            compact = {**detail}
            for field in ("origins", "changes", "replacements"):
                values = detail[field]
                if not isinstance(values, list):
                    raise ValueError("Invalid memory inspection history")
                compact[field + "_count"] = len(values)
                compact[field] = values[:8]
            record = {"text": json.dumps(compact, ensure_ascii=False)}
        else:
            values = data["arguments"]
            versions = data["expected_versions"]
            if not isinstance(values, dict) or not isinstance(versions, dict):
                raise ValueError("Invalid prepared memory mutation")
            common = {"request_id": "tool-" + digest([key, self.action]),
                      "session_id": session, "user_message_id": data["user_message_id"]}
            if self.action == "correct":
                request = Correction.model_validate({**common, **values,
                    "expected_fact_version": versions[values["memory_id"]]})
                record = await self.runtime.correct(request)
            elif self.action == "merge":
                request = Merge.model_validate({**common, **values, "expected_fact_versions": versions})
                record = await self.runtime.merge(request)
            else:
                request = SourceRetraction.model_validate({**common, **values})
                record = await self.runtime.retract_sources(request)
        if self.action in {"correct", "merge"}:
            memory_ids = record.get("memory_ids", [])
            if not isinstance(memory_ids, list):
                raise ValueError("Invalid memory mutation result")
            record = {**record, "text": str(record["text"]) + "\n新记忆 ID：" + "、".join(str(value) for value in memory_ids)}
        saved = await run_io(self.runtime.store.save_receipt, identity, signature,
                            {**record, "session_id": session, "graph": graph})
        return self.result(saved)
