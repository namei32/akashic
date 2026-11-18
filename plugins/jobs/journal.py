"""Wake receipts survive the host. Never persist or reconstruct a callable process."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from hashlib import sha256
from typing import cast

from plugins.ledger.contract import OwnerStore, OwnerTransaction
from .contract import JobOutcome, JobStatus, JobWatch


def receipt_key(job: JobWatch) -> str:
    return "job:" + sha256((job.session_id + "\0" + job.wake_source).encode()).hexdigest()


def _index(tx: OwnerTransaction, key: str, *, pending: bool) -> None:
    shard = "pending:" + key[4:6]
    record = tx.read(shard)
    entries = {} if record is None else dict(record.value)
    if pending:
        entries[key] = True
    else:
        entries.pop(key, None)
    tx.save(shard, entries, expected_version=None if record is None else record.version)


class WakeJournal:
    def __init__(self, store: OwnerStore):
        self.store = store

    def watch(self, job: JobWatch, input_id: str) -> bool:
        key = receipt_key(job)
        def create(tx: OwnerTransaction) -> bool:
            if tx.read(key) is not None:
                return False
            tx.save(key, {"state": "watching", "input": input_id, "session": job.session_id,
                          "id": job.job_id, "effect": job.effect_key, "kind": job.kind,
                          "label": job.label, "hint": job.collect_hint}, expected_version=None)
            _index(tx, key, pending=True)
            return True
        return self.store.transact(create)

    def update(self, job: JobWatch, state: str, outcome: JobOutcome | None = None) -> None:
        key = receipt_key(job)
        def save(tx: OwnerTransaction) -> None:
            record = tx.read(key)
            if record is None or record.value["state"] == "done":
                return
            changes = {} if outcome is None else {"status": outcome.status, "detail": outcome.detail}
            tx.save(key, {**record.value, **changes, "state": state}, expected_version=record.version)
            if state == "done":
                _index(tx, key, pending=False)
        self.store.transact(save)

    def pending(self) -> Iterator[Mapping[str, object]]:
        for _, shard in self.store.scan(start="pending:", stop="pending;", limit=256):
            for key in shard.value:
                record = self.store.read(key)
                if record is not None and record.value["state"] != "done":
                    yield record.value

    @staticmethod
    def restore(value: Mapping[str, object]) -> tuple[JobWatch, JobOutcome]:
        async def disconnected() -> None:
            return None
        job = JobWatch(str(value["session"]), "conversation", str(value["id"]), str(value["kind"]),
                       str(value["label"]), "根据已保存的结果核对；原进程句柄可能已失效。",
                       disconnected, effect_key=str(value["effect"]))
        outcome = (JobOutcome(cast(JobStatus, value["status"]), str(value["detail"])) if "status" in value
                   else JobOutcome("unknown", "宿主重启，进程终态未知。核对已保存的工具结果与外部证据，不重跑原操作。"))
        return job, outcome
