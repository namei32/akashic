from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from datetime import datetime

from agent.plugin_composition import Context
from agent.plugin_composition.plugin_config import PLUGIN_CONFIG
from plugins.drift.contract import DRIFT_CHANGED, DRIFT_DELIVERY_V2, DRIFT_WAKE_V3, INITIATIVE_CLOCK, InitiativeClock
from plugins.wake.contract import WAKE_CONTACT, WAKE_MEMORY_READY

from core.common.file_io import run_file_io

from .store import DriftStore
from .controls import mount, mount_configuration
from .initiative import Initiative
from .producer import offer
from .schema import ProducerConfig

api_version = 3
name = "drift"
version = "3.2.0"
desc = "Durable Drift proposal state"
author = "Akashic Core"
inject = ()
workspace_roots = ()
workspace_files = ()


class _AsyncWakeServices:
    def __init__(self, store: DriftStore, initiative: Initiative) -> None:
        self._store, self._initiative = store, initiative

    async def snapshot(self, now: datetime, *, target: Mapping[str, object] | None = None) -> Mapping[str, object]:
        return await run_file_io(lambda: self._store.snapshot(now, target=target))

    async def select(
        self,
        ref: Mapping[str, object],
        accepted_turn: Mapping[str, object],
        now: datetime,
        *, target: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        return await run_file_io(lambda: self._store.select(ref, accepted_turn, now, target=target))

    def send_start(self, accepted_turn: Mapping[str, object],
                   target: Mapping[str, object]) -> AbstractAsyncContextManager[str | None]:
        return self._initiative.start(accepted_turn, target)

    async def transition(self, token: str, action: str) -> Mapping[str, object]:
        return await run_file_io(lambda: self._store.transition(token, action))

    async def selection(
        self, accepted_turn: Mapping[str, object]
    ) -> Mapping[str, object] | None:
        return await run_file_io(lambda: self._store.selection(accepted_turn))


class _AsyncDeliveryServices:
    def __init__(self, store: DriftStore) -> None:
        self._store = store

    async def lookup(
        self, accepted_turn: Mapping[str, object]
    ) -> Mapping[str, object] | None:
        return await run_file_io(lambda: self._store.delivery(accepted_turn))

    async def settle(self, selection_token: str, settlement_ref: str) -> Mapping[str, object]:
        return await run_file_io(lambda: self._store.settle_delivery(selection_token, settlement_ref))


async def apply(ctx: Context) -> None:
    """Publish the narrow Drift view over one generation-scoped store."""

    store = DriftStore(ctx.data_root / "drift.sqlite3")
    await run_file_io(store.initialize)
    initiative = Initiative(store, lambda: ctx.emit(DRIFT_CHANGED, None))
    await mount(ctx, store, initiative)
    _ = await ctx.provide(DRIFT_WAKE_V3, _AsyncWakeServices(store, initiative))
    _ = await ctx.provide(DRIFT_DELIVERY_V2, _AsyncDeliveryServices(store))
    await ctx.inject((PLUGIN_CONFIG,), mount_configuration, name="configuration")
    config = ProducerConfig.model_validate(ctx.config)
    if config.auto_propose:
        async def producer(child: Context) -> None:
            async def tick(now: datetime, purpose: str) -> None:
                await offer(child, config, initiative, now, purpose)
            clock = InitiativeClock(config.interval_seconds, child.entrypoint(tick))
            class ClockPort:
                def open(self, consumer: Context) -> InitiativeClock:
                    if consumer.require_runtime_identity(INITIATIVE_CLOCK, self).plugin_id != "watch":
                        raise PermissionError("只有值班 owner 可以请求定时机会")
                    return clock
            await child.provide(INITIATIVE_CLOCK, ClockPort())
        dependencies = (WAKE_CONTACT, WAKE_MEMORY_READY) if config.memory_assisted else (WAKE_CONTACT,)
        await ctx.inject(dependencies, producer, name="producer")
