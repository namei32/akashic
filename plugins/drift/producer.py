"""An opt-in timer supplies conversation opportunities to the durable Drift queue."""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
import hashlib
import json
from zoneinfo import ZoneInfo

from agent.plugin_composition import Context
from core.common.file_io import run_file_io
from plugins.wake.contract import WAKE_CONTACT

from .initiative import Initiative
from .schema import ProducerConfig


async def offer(ctx: Context, config: ProducerConfig, initiative: Initiative, now: datetime, purpose: str) -> None:
    contact = ctx.require(WAKE_CONTACT)()
    zone = ZoneInfo(contact.timezone)
    local = now.astimezone(zone)
    if not config.start_hour <= local.hour < config.end_hour:
        return
    target = contact.target()
    identity = json.dumps([now.isoformat(), target], sort_keys=True, separators=(",", ":"))
    proposal_id = "initiative:" + hashlib.sha256(identity.encode()).hexdigest()
    window_end = datetime.combine(local.date(), time.min, tzinfo=zone) + timedelta(hours=config.end_hour)
    valid_until = min(now + timedelta(hours=1), window_end.astimezone(UTC))
    payload: dict[str, object] = {"kind": "conversation", "purpose": purpose,
                                  "valid_until": valid_until.isoformat()}
    if config.memory_assisted:
        consent = await run_file_io(initiative.store.memory_consent)
        payload.update(memory_assisted=True, consent_version=consent["consent_version"])
    await initiative.write(lambda: initiative.store.propose(
        proposal_id, "1", payload, now, initiative_target=target))
