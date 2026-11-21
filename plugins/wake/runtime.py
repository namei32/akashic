from __future__ import annotations

from plugins.delivery.contract import DELIVERY_SCOPED_READ

import asyncio
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import cast

from core.common.file_io import run_file_io

from agent.plugin_composition import Context
from plugins.channels.contract import private_target_key
from agent.plugin_composition.plugin_config import PLUGIN_CONFIG
from plugins.ledger.contract import BINDINGS
from plugins.ledger.contract import (
    MESSAGE_CATALOG,
    MessageReader,
    OwnerRecord,
)
from plugins.ledger.contract import Message, body_to_dict
from plugins.models.contract import (
    MODEL_SELECTION as MODEL_SELECTION,
    ModelSelection as ModelSelection,
)

from ._boundary import (
    ALL_TOOLS,
    CONTENT,
    DELIVERY_READ,
    DELIVERY_SENDERS,
    TOOLS,
    WAKE_TOOLS_VIEW,
    SinkValue,
    ToolView,
)
from .admission import Admission, Pool
from .content import _integer, _sequence
from .active_sources import ActiveSources
from .api import DRIFT_WAKE, EVENTMAIL_WAKE, Config, DeliveryTarget
from .legacy_rules import read_archived_rules
from .messages import recent_context
from .initiative_memory import capture as capture_memory_context
from .request import TOOLS as WAKE_TOOLS, WAKE_PROGRAM, Request
from .selection import propose_drift
from .source import Pointer, Source
from .state import WakeState, WakeStateReader


class DashboardView:
    """Expose Wake's durable rows and original Message flow as a read-only view."""

    def __init__(
        self,
        state: WakeStateReader,
        read_flow: Callable[[str], tuple[OwnerRecord, Request, MessageReader] | None],
        read_message: Callable[[str, str], Message | None],
        delivery_status: Callable[[str, str], Mapping[str, object] | None],
    ) -> None:
        self._state = state
        self._read_flow = read_flow
        self._read_message = read_message
        self._delivery_status = delivery_status

    def list_attempts(self, limit: int, *, offset: int = 0) -> tuple[Mapping[str, object], ...]:
        return self._state.list_attempts(limit, offset=offset)

    def count_attempts(self) -> int:
        return self._state.count_attempts()

    def get_attempt(self, attempt_id: str) -> Mapping[str, object] | None:
        row = self._state.get_attempt(attempt_id)
        if row is None:
            return None
        return {**row, "flow": self.flow(attempt_id)}

    def list_runs(self, limit: int, *, offset: int = 0) -> tuple[Mapping[str, object], ...]:
        return self._state.list_runs(limit, offset=offset)

    def count_runs(self) -> int:
        return self._state.count_runs()

    def get_run(self, run_id: str) -> Mapping[str, object] | None:
        row = self._state.get_run(run_id)
        if row is None:
            return None
        return {**row, "flow": self.flow(run_id)}

    def flow(self, flow_id: str) -> Mapping[str, object] | None:
        found = self._read_flow(flow_id)
        if found is None:
            return None
        pointer_row, request, reader = found
        pointer = Pointer.model_validate(dict(pointer_row.value))
        messages = tuple(self._message(message) for message in reader.snapshot())
        # Wake's request lives in an internal session. The notification is
        # deliberately written to the target session, so read that catalog.
        notification = self._read_message(request.target.session_id, request.notification_id)
        delivery: dict[str, object] = {
            "message_id": request.notification_id,
            "channel": request.target.channel,
            "recipient": request.target.recipient,
            "status": "not_published",
            "receipt": None,
        }
        if notification is not None:
            status = self._delivery_status(notification.message_id, request.target.channel)
            if status is None:
                delivery["status"] = "not_prepared"
            else:
                delivery.update(status)
        return {
            "flow_id": flow_id,
            "pointer": {"version": pointer_row.version, **pointer.model_dump(mode="json")},
            "request": {
                **request.model_dump(mode="json", exclude={"program_binding", "tools", "rules", "history", "events"}),
                "session_id": request.session_id,
                "input_id": request.input_id,
                "notification_id": request.notification_id,
            },
            "messages": list(messages),
            "delivery": delivery,
        }

    @staticmethod
    def _message(message: Message) -> Mapping[str, object]:
        body = body_to_dict(message.body)
        parts = body.get("parts", ()) if isinstance(body, dict) else ()
        text = "\n".join(
            str(part.get("value"))
            for part in parts
            if isinstance(part, dict) and part.get("kind") == "text" and isinstance(part.get("value"), str)
        ) if isinstance(parts, list) else ""
        return {
            "message_id": message.message_id,
            "session_id": message.session_id,
            "seq": message.seq,
            "recorded_at": message.recorded_at.isoformat(),
            "author": message.author,
            "source": message.source,
            "body": body,
            "text": text,
        }


class Runtime:
    """只执行或恢复已接纳的职责；值班时机由 Watch 独占。"""

    def __init__(self, ctx: Context, config: Config, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.ctx, self.config, self.now = ctx, self._current_config(config), now
        self.state = WakeState(ctx.data_root / "wake.sqlite3")
        # apply/follow 拥有异步初始化；构造函数不在共享 loop 执行 SQL。
        self.source = Source(ctx, self.state, now=now)
        self.active_sources = ActiveSources()

    @staticmethod
    def _current_config(config: Config) -> Config:
        """把跨 generation 的配置值重建为当前 Wake 模块的类型。"""
        target = config.delivery
        if type(config) is Config and (target is None or type(target) is DeliveryTarget):
            return config
        return Config.model_validate(config.model_dump(mode="python"))

    async def capture(self, flow_id: str, admission: Admission, now: datetime, *,
                      alert_ref: Mapping[str, object] | None = None, watch_private: bool = False) -> Request | None:
        """先固定归档程序、工具、出站目标与上下文，随后才允许领取领域条目。"""
        owner = admission.owner
        if owner is None:
            return None
        ctx, target = self.ctx, self.config.delivery
        if target is None:
            return None
        alert = alert_ref if alert_ref is not None else (
            await ctx.require(EVENTMAIL_WAKE).peek_alert(now) if owner == "alert" else None)
        if owner == "alert" and alert is None:
            return None
        bindings = ctx.require(BINDINGS)
        sink: SinkValue = {
            "name": target.channel,
            "address": target.recipient,
            "binding_id": ctx.require(DELIVERY_SENDERS).bind(target.channel, bindings),
        }
        metadata = ctx.require(MESSAGE_CATALOG).reader(target.session_id).metadata()
        model = ctx.require(MODEL_SELECTION).read_saved(metadata if metadata is not None else {})
        catalog = ctx.require(TOOLS)
        allowed = set(self.config.investigation_tools) if owner == "content" and not watch_private else set()
        view: ToolView = catalog.view(
            *ctx.require(WAKE_TOOLS_VIEW).refs,
            *(ref for ref in ctx.require(ALL_TOOLS)().refs if ref.name in allowed),
        )
        names = (*WAKE_TOOLS[owner], *(ref.name for ref in view.refs
                 if ref.name in allowed and ref.name not in WAKE_TOOLS[owner]))
        auto_sources = owner == "drift" and self.config.active_sources.enabled
        events = () if auto_sources or (watch_private and owner != "drift") else tuple(dict(item) for item in await ctx.require(EVENTMAIL_WAKE).active_context(now))
        proposal = propose_drift(admission.proposals) if owner == "drift" else None
        memory_assisted = proposal is not None and proposal.payload.get("memory_assisted") is True
        memory_query, memory_blocked, source_seq = "", "", None
        history = ""
        if memory_assisted and not auto_sources:
            with ctx.borrow(DELIVERY_SCOPED_READ) as scoped:
                if scoped is None:
                    history, memory_blocked = "", "scoped_history_unavailable"
                else:
                    messages, content = ctx.require(MESSAGE_CATALOG), ctx.require(CONTENT)
                    scoped_history = scoped
                    source_seq, memory_query, history, memory_blocked = await run_file_io(
                        lambda: capture_memory_context(messages, content, scoped_history, session_id=target.session_id, now=now),
                    )
            if not memory_blocked and not memory_query:
                memory_blocked = "no_user_input"
            if events and not memory_blocked:
                memory_blocked = "unscoped_context"
            events = ()
        elif watch_private and not auto_sources:
            with ctx.borrow(DELIVERY_SCOPED_READ) as scoped:
                if scoped is not None:
                    messages, content = ctx.require(MESSAGE_CATALOG), ctx.require(CONTENT)
                    _, _, history, _ = await run_file_io(lambda: capture_memory_context(
                        messages, content, scoped, session_id=target.session_id, now=now))
        elif not auto_sources:
            history = recent_context(ctx.require(MESSAGE_CATALOG), ctx.require(DELIVERY_READ),
                                     target=target.session_id, now=now)
        source_policy, source_evidence, memory_source_session, rejected = "", (), "", []
        if auto_sources:
            source_evidence, passive, queries, rejected = await self.active_sources.capture(
                ctx, self.config.active_sources, target, now)
            source_policy = self.config.active_sources.fingerprint()
            # Recall exactly one source scope; do not blend several provider queries.
            if memory_assisted and source_evidence:
                memory_source_session = source_evidence[0].session_id
                memory_query = queries[memory_source_session]
                source_seq = source_evidence[0].through_seq
                memory_blocked = "" if memory_blocked == "no_user_input" else memory_blocked
            # Source evidence is archived separately; never truncate the JSON document.
            history = json.dumps({"recent_conversation": passive, "delivered_messages": []}, ensure_ascii=False)
        return Request(
            flow_id=flow_id,
            owner=owner,
            now=now,
            timezone=self.config.timezone,
            target=target,
            sink=sink,
            program_binding=bindings.bind(WAKE_PROGRAM, {}),
            tools={
                name: await catalog.bind_scoped(view.select(name), bindings)
                for name in names
            },
            snapshot_seq=admission.pool.snapshot_seq,
            items=tuple(dict(item) for item in admission.pool.items),
            proposals=tuple(dict(item) for item in admission.proposals),
            alert_ref=None if alert is None else cast(dict[str, str], dict(alert)),
            model_id=model.model_id, reasoning_effort=model.reasoning_effort,
            rules=read_archived_rules(ctx.data_root) or "",
            history=history, events=events, memory_assisted=memory_assisted,
            source_policy=source_policy, source_evidence=source_evidence, source_rejections=tuple(rejected),
            source_config_revision=str(ctx.require(PLUGIN_CONFIG).read(ctx)["config_revision"]) if source_policy else "",
            memory_source_session=memory_source_session,
            memory_provider=self.config.memory_provider,
            watch_private=watch_private,
            memory_query=memory_query, memory_source_seq=source_seq, memory_blocked=memory_blocked)

    async def recover(self) -> None:
        await run_file_io(lambda: self.state.close_interrupted_attempts(self.now()))
        for flow_id in self.source.pending():
            found = self.source.read(flow_id)
            if found is not None and not found[1].watch_private:
                await self._run(flow_id)

    async def dispatch(self, flow_id: str, kind: str, ref: Mapping[str, object]) -> str:
        target = self.config.delivery
        if target is None or kind not in {"content", "drift", "alert"}:
            raise ValueError("无有效的主动联系目标或事件类型")
        existing = self.source.read(flow_id)
        if existing is not None:
            original = existing[1]
            if not original.watch_private or original.target != target or original.owner != kind:
                raise ValueError("Watch 请求的原目标或职责已变化")
            return await self._dispatch_result(flow_id)
        now = self.now()
        with self.ctx.borrow(private_target_key(target.channel)) as verifier:
            if verifier is None:
                return "pending"
            proof = verifier.guard(target.recipient, target.session_id)
        async with proof as private:
            if not private:
                return "target_unavailable"
        pool = Pool(0, (), 0, 0, 0, 0)
        mail = self.ctx.require(EVENTMAIL_WAKE)
        if kind == "content":
            snapshot = await mail.snapshot(now)
            items = tuple(item for item in _sequence(snapshot.get("items"), "items") if item["ref"] == ref and item["due"])
            if not items:
                return "stale"
            admission = Admission("content", "Watch", Pool(_integer(snapshot.get("snapshot_seq"), "snapshot_seq"), items, 1, 1, 0, 0))
        elif kind == "drift":
            snapshot = await self.ctx.require(DRIFT_WAKE).snapshot(now, target=target.model_dump())
            proposals = tuple(item for item in _sequence(snapshot.get("proposals"), "proposals") if item["ref"] == ref and item["due"])
            if not proposals:
                return "stale"
            admission = Admission("drift", "Watch", pool, proposals)
        else:
            if set(ref) != {"source_id", "event_id", "mail_id"}:
                raise ValueError("Watch 告警必须绑定原始信封")
            if await mail.alert_status(str(ref["source_id"]), str(ref["event_id"]), mail_id=str(ref["mail_id"])) != "pending":
                return "stale"
            admission = Admission("alert", "Watch", pool)
        request = await self.capture(flow_id, admission, now, alert_ref=ref if kind == "alert" else None,
                                     watch_private=True)
        if request is None:
            return "stale"
        await self.source.accept(request)
        return await self._dispatch_result(flow_id)

    async def _dispatch_result(self, flow_id: str) -> str:
        result = await self._run(flow_id)
        found = self.source.read(flow_id)
        if found is not None and not Pointer.model_validate(dict(found[0].value)).settled:
            return "pending"
        return result or "settled"


    async def _run(self, flow_id: str) -> str | None:
        """来源循环关闭时撤销并排空自己接纳的 Task，保留原 Input 供新进程恢复。"""
        task = await self.source.start(flow_id)
        if task is None:
            return None
        try:
            return cast(str, await task.join())
        except asyncio.CancelledError:
            task.cancel()
            while not task.done:
                try:
                    _ = await task.join()
                except asyncio.CancelledError:
                    pass
            raise
