"""Deliver a host release report to the existing Session of its original call."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from agent.plugin_composition import Context
from plugins.gateway.contract import RpcMethod
from plugins.ledger.contract import MESSAGE_CATALOG, MESSAGE_WRITERS, ContentPart, Output, ToolCall

from .boundary import CONTENT


class ReleaseReport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    boot_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=512)
    call_message_id: str = Field(min_length=1, max_length=256)
    call_part_index: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=2400)


async def register_release_report(ctx: Context) -> None:
    async def report(params: BaseModel) -> object:
        assert isinstance(params, ReleaseReport)
        fields = params.model_dump(include={"boot_id", "session_id", "call_message_id", "call_part_index"})
        if hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:32] != params.request_id:
            raise ValueError("发布报告与原接单身份不符")
        catalog = ctx.require(MESSAGE_CATALOG)
        if not catalog.exists(params.session_id):
            raise ValueError("发布报告的原会话不存在")
        reader = catalog.reader(params.session_id)

        def check_call():
            call = reader.get(params.call_message_id)
            if (reader.deleted or call is None or not isinstance(call.body, Output)
                    or params.call_part_index >= len(call.body.parts)
                    or not isinstance(call.body.parts[params.call_part_index], ToolCall)):
                raise ValueError("发布报告缺少原会话的工具调用")

        await reader.read_async(lambda _: check_call())
        writer = ctx.require(MESSAGE_WRITERS).bind(
            ctx, author="assistant", source="release_report", body_types=(Output,),
            content={"text": ctx.require(CONTENT).check_text},
        )(params.session_id)
        identity = "release-report:" + params.request_id + ":" + params.event_id
        try:
            message = await writer.append_async(identity, Output((ContentPart("text", params.text),), "complete"))
        finally:
            writer.expire()
        return {"status": "committed", "session_id": message.session_id, "message_id": message.message_id}

    await ctx.provide(RpcMethod.key("runtime/release-report"), RpcMethod(ReleaseReport, report))
