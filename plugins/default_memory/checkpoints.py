"""Learn only completed turns actually covered by a published compaction input."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import cast

from plugins.compaction.contract import COMPACTION_SUMMARIES, PartitionedSummary, StoredSummary, SummaryLookup
from plugins.context.contract import ContextBuilder
from plugins.ledger.contract import Bindings, ContentPart, MessageCatalog, Output
from .io import run_io
from .schema import Source
from .store import digest


async def collect(catalog: MessageCatalog, bindings: Bindings, context: ContextBuilder,
                  summaries: SummaryLookup, turns: Sequence[Source], heads: Mapping[str, int],
                  *, batch_turns: int = 20) -> tuple[Source, ...]:
    by_session: dict[str, list[Source]] = {}
    for turn in turns:
        by_session.setdefault(turn.session_id, []).append(turn)
    result: list[Source] = []
    for session, candidates in by_session.items():
        reader = catalog.reader(session)
        snapshot = await run_io(lambda: reader.snapshot(through_seq=heads[session]))
        records: dict[str, StoredSummary] = {}
        # A prepared-but-unused summary is not a learning event.
        for message in snapshot:
            if not isinstance(message.body, Output):
                continue
            for part in message.body.parts:
                if not isinstance(part, ContentPart) or part.kind != "context.summary":
                    continue
                refs = context.check_summary(part).binding_ids
                if len(refs) != 1:
                    raise ValueError("Compaction material must identify exactly one checkpoint")
                # Full lineage validation can read a long chain; keep it off the event loop.
                metadata = await run_io(bindings.describe, refs[0], COMPACTION_SUMMARIES)
                record = await run_io(summaries.resolve, metadata, session_id=session)
                records[record.reference] = record
        model_inputs: dict[str, set[str]] = {}
        async def covered(record: StoredSummary, visiting: frozenset[str] = frozenset()) -> set[str]:
            if record.reference in visiting:
                raise ValueError("Compaction parent cycle")
            if record.reference in model_inputs:
                return model_inputs[record.reference]
            parent = None if record.parent is None else await run_io(
                summaries.resolve, {"record_ref": record.parent, "session_id": session}, session_id=session)
            previous = set() if parent is None else await covered(parent, visiting | {record.reference})
            delta = (cast(PartitionedSummary, record).summary_message_ids if record.version == 2
                     else record.source_message_ids[0 if parent is None else len(parent.source_message_ids):])
            value = previous | set(delta)
            model_inputs[record.reference] = value
            return value
        admitted: set[str] = set()
        for record in sorted(records.values(), key=lambda row: row.generation):
            allowed = await covered(record)
            ready = [turn for turn in candidates if turn.key not in admitted and set(turn.message_ids) <= allowed]
            admitted.update(turn.key for turn in ready)
            for start in range(0, len(ready), batch_turns):
                batch = ready[start:start + batch_turns]
                if not batch:
                    continue
                ids = tuple(dict.fromkeys(identity for turn in batch for identity in turn.message_ids))
                users = tuple(dict.fromkeys(identity for turn in batch for identity in turn.user_ids))
                rows = [row for turn in batch for row in json.loads(turn.text)]
                result.append(Source(
                    "checkpoint:" + digest([record.reference, [turn.key for turn in batch]]),
                    batch[0].graph, session, batch[-1].ending_id, batch[-1].timestamp,
                    ids, users, digest([record.reference, [turn.digest for turn in batch]]),
                    json.dumps(rows, ensure_ascii=False),
                ))
    return tuple(result)
