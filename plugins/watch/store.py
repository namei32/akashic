from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .watcher import Event, Verdict


def encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class Candidate:
    kind: str
    ref: Mapping[str, object]
    event: Event

    @classmethod
    def create(cls, kind: str, ref: Mapping[str, object], payload: Mapping[str, object], at: str) -> Candidate:
        identity = hashlib.sha256(encoded([kind, ref]).encode()).hexdigest()
        source = str(ref.get("source_id") or kind)
        title = str(payload.get("title") or payload.get("purpose") or "")
        text = next((str(payload[key]) for key in ("summary", "text", "message", "description", "body")
                     if isinstance(payload.get(key), str)), "")
        return cls(kind, ref, Event(identity, source, at, title[:500], text[:2000], ""))


class Journal:
    def __init__(self, root: Path):
        self.path = root / "watch.sqlite3"

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS decisions(
                    target TEXT NOT NULL, event TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL,
                    title TEXT NOT NULL, decided_at TEXT NOT NULL, route TEXT NOT NULL, reason TEXT NOT NULL,
                    outcome TEXT, PRIMARY KEY(target,event));
                CREATE INDEX IF NOT EXISTS pending ON decisions(target, outcome, route);
                CREATE INDEX IF NOT EXISTS bound_event ON decisions(event);
                CREATE TABLE IF NOT EXISTS retries(target TEXT NOT NULL,event TEXT NOT NULL,tried TEXT NOT NULL,
                    PRIMARY KEY(target,event));
                CREATE TABLE IF NOT EXISTS clocks(target TEXT PRIMARY KEY, offered_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ticks(id INTEGER PRIMARY KEY, target TEXT NOT NULL,
                    started TEXT NOT NULL, finished TEXT, detail TEXT);
            """)
            db.execute("UPDATE ticks SET detail='检查中断，尚无完成回执' WHERE finished IS NULL AND detail IS NULL")

    def start(self, target: str, now: datetime) -> int:
        with closing(sqlite3.connect(self.path)) as db, db:
            cursor = db.execute("INSERT INTO ticks(target,started) VALUES (?,?)", (target, now.isoformat()))
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    def finish(self, tick: int, now: datetime, detail: str) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE ticks SET finished=?,detail=? WHERE id=?", (now.isoformat(), detail, tick))

    def offer_due(self, target: str, now: datetime, interval: int) -> datetime | None:
        with closing(sqlite3.connect(self.path)) as db, db:
            previous = db.execute("SELECT offered_at FROM clocks WHERE target=?", (target,)).fetchone()
            if previous and (now - datetime.fromisoformat(previous[0])).total_seconds() < interval:
                return None
            db.execute("INSERT OR REPLACE INTO clocks VALUES (?,?)", (target, now.isoformat()))
            return datetime.fromisoformat(previous[0]) if previous else None

    def unseen(self, candidates: Iterable[Candidate], limit: int) -> list[Candidate]:
        with closing(sqlite3.connect(self.path)) as db:
            result = []
            for item in candidates:
                if not db.execute("SELECT 1 FROM decisions WHERE event=?", (item.event.id,)).fetchone():
                    result.append(item)
                    if len(result) == limit:
                        break
            return result

    def record(self, target: str, now: datetime, candidates: Sequence[Candidate], verdicts: Sequence[Verdict]) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            for item, verdict in zip(candidates, verdicts, strict=True):
                if verdict.event.id != item.event.id:
                    raise ValueError("Watch 判决与本批身份不一致")
                db.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,NULL)", (
                    target, item.event.id, item.kind, encoded(item.ref), item.event.title,
                    now.isoformat(), verdict.to, verdict.why))

    def pending(self, target: str, before: str, limit: int) -> list[tuple[str, str, dict[str, object]]]:
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("""SELECT event,kind,ref FROM decisions LEFT JOIN retries USING(target,event)
                WHERE target=? AND outcome IS NULL
                AND (route='wake' OR (route='inbox' AND decided_at<=?))
                ORDER BY (tried IS NOT NULL), tried, decisions.rowid LIMIT ?""", (target, before, limit))
            return [(event, kind, json.loads(ref)) for event, kind, ref in rows]

    def tried(self, target: str, event: str, now: datetime) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT OR REPLACE INTO retries VALUES (?,?,?)", (target, event, now.isoformat()))

    def settle(self, target: str, event: str, result: str) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE decisions SET outcome=? WHERE target=? AND event=? AND outcome IS NULL",
                       (result, target, event))

    def panel(self, target: str) -> dict[str, object]:
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return {"events": [dict(row) for row in db.execute(
                "SELECT title,kind,decided_at,route,reason,outcome FROM decisions WHERE target=? ORDER BY rowid DESC LIMIT 80", (target,))],
                    "ticks": [dict(row) for row in db.execute(
                "SELECT started,finished,detail FROM ticks WHERE target=? ORDER BY id DESC LIMIT 12", (target,))]}
