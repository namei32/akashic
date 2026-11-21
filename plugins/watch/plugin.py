from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from agent.plugin_composition import Context
from core.common.file_io import run_file_io
from plugins.channels.contract import private_target_key
from plugins.drift.contract import DRIFT_WAKE_V3, INITIATIVE_CLOCK, InitiativeClock
from plugins.eventmail.contract import EVENTMAIL_WAKE_V2, WATCH_ALERTS
from plugins.models.contract import CHAT_MODELS
from plugins.ui.contract import UI_SLOTS, PluginUiDefinition, PluginUiNavigation, PluginUiRpcInvalidRequest
from plugins.wake.contract import SCOPED_CONTACT, WAKE_CONTACT, ScopedContact
from pydantic import BaseModel, ConfigDict, Field

from . import orders
from .store import Candidate, Journal, encoded
from .watcher import classify

api_version = 3
name = "watch"
version = "1.0.0"
desc = "受限值班：判断现有来源、留待稍后或交给原授权的投递流程"
inject = ()


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_seconds: int = Field(default=60, ge=10, le=3600)
    digest_hours: int = Field(default=4, ge=0, le=72)
    model: str | None = None
    orders: str = Field(default="", max_length=8000)


def entries(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(row, Mapping) for row in value):
        raise ValueError("来源缺少有效的事件清单")
    return list(value)


def candidates(rows: Sequence[Mapping[str, object]], kind: str, now: datetime) -> list[Candidate]:
    result = []
    for row in rows:
        if kind != "alert" and row.get("due") is not True:
            continue
        ref, payload = row.get("ref"), row.get("payload")
        if not isinstance(ref, Mapping) or not isinstance(payload, Mapping):
            raise ValueError("来源缺少原始事件身份或正文")
        if kind == "content" and payload.get("wake_eligible") is False:
            continue
        result.append(Candidate.create(kind, ref, payload, str(row.get("observed_at") or row.get("due_at") or now.isoformat())))
    return result


async def tick(ctx: Context, journal: Journal, config: Config, now: datetime, contact: ScopedContact,
               clock: InitiativeClock | None) -> str:
    target = contact.target
    key = encoded(target.target())
    with ctx.borrow(private_target_key(target.channel)) as verifier:
        if verifier is None:
            return "无私人目标证明，未取材"
        proof = verifier.guard(target.recipient, target.session_id)
    async with proof as private:
        if not private:
            return "目标不再是私人会话，未取材"
        local = now.astimezone(ZoneInfo(target.timezone))
        instructions = [order for order in orders.parse(config.orders) if order.until is None or order.until >= local.date()]
        if clock is not None:
            interval = clock.interval_seconds
            previous = await run_file_io(lambda: journal.offer_due(key, now, interval))
            if previous is not None:
                after = max(previous, now - timedelta(seconds=interval)).astimezone(local.tzinfo)
                timed = [order.text for order in instructions if orders.due(order, after, local)]
                await clock.offer(now, "到点查看：" + "；".join(timed) if timed else "考虑是否自然地与用户搭话")
        mail = ctx.require(EVENTMAIL_WAKE_V2)
        alerts = await ctx.require(WATCH_ALERTS)(now)
        proposals = await ctx.require(DRIFT_WAKE_V3).snapshot(now, target=target.target())
        content = await mail.snapshot(now)
        items = (candidates(alerts, "alert", now) + candidates(entries(proposals.get("proposals")), "drift", now)
                 + candidates(entries(content.get("items")), "content", now))
        batch = await run_file_io(lambda: journal.unseen(items, 24))
        classification_error: Exception | None = None
        if batch:
            standing, _ = orders.render([order for order in instructions if not order.when])
            standing += "\n已到点的主动交流机会由原有定时授权产生，交原执行器判断是否开口；不要自行生成交流机会。"
            try:
                async with asyncio.timeout(90):
                    results = await classify(ctx.require(CHAT_MODELS), [item.event for item in batch], standing, config.model)
                await run_file_io(lambda: journal.record(key, now, batch, results))
            except (ValueError, RuntimeError, TimeoutError) as error:
                classification_error = error
    before = (now - timedelta(hours=config.digest_hours)).isoformat() if config.digest_hours else ""
    pending = await run_file_io(lambda: journal.pending(key, before, 8))
    for event, kind, ref in pending:
        flow = hashlib.sha256(encoded([key, event]).encode()).hexdigest()[:32]
        await run_file_io(lambda: journal.tried(key, event, now))
        result = await contact.dispatch(flow, kind, ref)
        if result != "pending":
            await run_file_io(lambda: journal.settle(key, event, result))
    if classification_error is not None:
        raise classification_error
    return f"判断 {len(batch)} 条；移交 {len(pending)} 条"


async def apply(ctx: Context) -> None:
    config = Config.model_validate(ctx.config)
    journal = Journal(ctx.data_root)
    await run_file_io(journal.initialize)
    clock: InitiativeClock | None = None

    async def attach_clock(child: Context) -> None:
        bound = child.require(INITIATIVE_CLOCK).open(child)
        def attach():
            nonlocal clock
            clock = bound
            def close():
                nonlocal clock
                clock = None
            return close
        await child.effect(attach, label="scoped-clock")
    await ctx.inject((INITIATIVE_CLOCK,), attach_clock, name="clock")

    async def worker(child: Context) -> None:
        contact = child.require(SCOPED_CONTACT).open(child)
        async def follow() -> None:
            while True:
                async with child.runtime_scope():
                    now = datetime.now(UTC)
                    sequence = await run_file_io(lambda: journal.start(encoded(contact.target.target()), now))
                    try:
                        await contact.recover()
                        detail = await tick(child, journal, config, now, contact, clock)
                    except Exception as error:
                        detail = f"失败：{type(error).__name__}: {str(error)[:240]}"
                        child.report_incident("watch_tick_failed", detail)
                    await run_file_io(lambda: journal.finish(sequence, datetime.now(UTC), detail))
                await asyncio.sleep(config.interval_seconds)
        await child.spawn(follow(), name="watch")

    await ctx.inject((SCOPED_CONTACT, DRIFT_WAKE_V3, WATCH_ALERTS, EVENTMAIL_WAKE_V2, CHAT_MODELS),
                     worker, name="contact")

    async def ui(child: Context) -> None:
        async def query(method: str, payload: dict[str, object], *, session_id: str | None, turn_id: str | None):
            if method != "recent" or payload:
                raise PluginUiRpcInvalidRequest("只支持 recent")
            with child.borrow(WAKE_CONTACT) as selected:
                if selected is None:
                    return {"events": [], "ticks": []}
                target = selected()
            with child.borrow(private_target_key(target.channel)) as verifier:
                if verifier is None:
                    return {"events": [], "ticks": []}
                proof = verifier.guard(target.recipient, target.session_id)
            async with proof as private:
                if not private:
                    return {"events": [], "ticks": []}
                return await run_file_io(lambda: journal.panel(encoded(target.target())))
        await child.require(UI_SLOTS).register_plugin_ui(child, PluginUiDefinition(module="panel.js",
            navigation=PluginUiNavigation(label="Watch 值班", description="查看事件去向、待看与移交结果")), query=query)
    await ctx.inject((UI_SLOTS,), ui, name="panel")
