"""Scoped inspection and version-preserving user-directed corrections."""
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC
import json
from typing import Literal

from .api import Browse, Correction, Merge, Similar
from .event_time import parse_event_time

from .schema import Kind
from .store import Store, digest



def browse(store: Store, graph: str, request: Browse) -> dict[str, object]:
    clauses = ["graph=?"]
    values: list[object] = [graph]
    if request.status != "all":
        clauses.append("status=?")
        values.append(request.status)
    if request.kind is not None:
        clauses.append("kind=?")
        values.append(request.kind)
    source_match = ("EXISTS (SELECT 1 FROM origins o WHERE o.memory_id=memory_items.id AND "
                    "(instr(lower(o.source_key),lower(?))>0 OR EXISTS "
                    "(SELECT 1 FROM json_each(o.evidence_ids) e WHERE instr(lower(e.value),lower(?))>0)))")
    if request.query:
        clauses.append("(instr(lower(id),lower(?))>0 OR "
                       "instr(lower(json_extract(fact,'$.summary')),lower(?))>0 OR " + source_match + ")")
        values.extend([request.query] * 4)
    if request.source_ref:
        clauses.append(source_match)
        values.extend([request.source_ref] * 2)
    if request.has_embedding is not None:
        clauses.append("json_array_length(embedding)" + (">0" if request.has_embedding else "=0"))
    where = " AND ".join(clauses)
    order = {
        "updated_at": "updated_at",
        "created_at": "created_at",
        "happened_at": "event_time_key(json_extract(fact,'$.happened_at'))",
        "reinforcement": "reinforcement",
        "emotional_weight": "coalesce(json_extract(fact,'$.emotional_weight'),0)",
        "kind": "kind",
    }[request.sort_by]
    direction = "ASC" if request.sort_order == "asc" else "DESC"

    def event_time_key(value: object) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError("Invalid stored event time")
        return parse_event_time(value).start.astimezone(UTC).isoformat()

    with store.connection() as db:
        db.execute("BEGIN")
        db.create_function("event_time_key", 1, event_time_key, deterministic=True)
        total = db.execute("SELECT count(*) FROM memory_items WHERE " + where, values).fetchone()[0]
        rows = db.execute("SELECT * FROM memory_items WHERE " + where +
                          " ORDER BY (" + order + " IS NULL) ASC," + order + " " + direction +
                          ",id ASC LIMIT ? OFFSET ?", [*values, request.limit, request.offset]).fetchall()
        items = []
        removed = store._retracted(db, graph)
        for row in rows:
            item = store.project(db, row, removed)
            items.append({**{k: v for k, v in asdict(item).items() if k != "vector"},
                          "fact": item.fact.model_dump(mode="json"), "has_embedding": bool(item.vector)})
    return {"items": items, "total": total, "offset": request.offset, "limit": request.limit}


def inspect(store: Store, graph: str, memory_id: str) -> dict[str, object]:
    with store.connection() as db:
        db.execute("BEGIN")
        row = db.execute("SELECT * FROM memory_items WHERE id=? AND graph=?", (memory_id, graph)).fetchone()
        if row is None:
            raise ValueError("Memory not found in this scope")
        removed = store._retracted(db, graph)
        item = store.project(db, row, removed)
        origins = []
        for source_key, raw, source in db.execute(
            "SELECT o.source_key,o.evidence_ids,l.source FROM origins o "
            "LEFT JOIN learning l ON l.source_key=o.source_key "
            "WHERE o.memory_id=? ORDER BY o.source_key,o.item_index", (memory_id,)):
            if source is None:
                raise ValueError("Memory origin has no learning source")
            refs = store._references(raw)
            context = store._source_users(source)
            origins.append({"source_key": source_key, "evidence_ids": list(refs),
                            "retracted": bool((set(refs) | set(context)) & removed)})
        changes = [{"event_key": r[0], "action": r[1], "reason": r[2], "evidence": json.loads(r[3])}
                   for r in db.execute("SELECT event_key,action,reason,evidence FROM changes WHERE memory_id=? ORDER BY event_key", (memory_id,))]
        replacements = [{"source_key": r[0], "previous_id": r[1], "current_id": r[2], "reason": r[3], "evidence": json.loads(r[4])}
                        for r in db.execute("SELECT * FROM replacements WHERE previous_id=? OR current_id=? ORDER BY source_key", (memory_id, memory_id))]
    value = {**{k: v for k, v in asdict(item).items() if k != "vector"}, "fact": item.fact.model_dump(mode="json")}
    retracted_origins = sorted(str(origin["source_key"]) for origin in origins if origin["retracted"])
    fact_version = (digest(json.loads(row["fact"])) if not retracted_origins
                    else digest([json.loads(row["fact"]), retracted_origins]))
    return {**value, "fact_version": fact_version, "origins": origins,
            "changes": changes, "replacements": replacements}




def similar(store: Store, graph: str, request: Similar) -> dict[str, object]:
    from .retrieval import cosine, normalized
    with store.connection() as db:
        db.execute("BEGIN")
        target = db.execute("SELECT * FROM memory_items WHERE graph=? AND id=?", (graph, request.memory_id)).fetchone()
        if target is None:
            raise ValueError("Memory not found in this scope")
        removed = store._retracted(db, graph)
        original = store.project(db, target, removed)
        dimension = store.dimension()
        if dimension is None:
            raise ValueError("Memory embedding space is missing")
        vector = normalized(original.vector, dimension)
        rows = db.execute("SELECT * FROM memory_items WHERE graph=? AND id<>? AND (? OR status='active')",
                          (graph, request.memory_id, request.include_superseded)).fetchall()
        scored = []
        for row in rows:
            item = store.project(db, row, removed)
            score = cosine(vector, normalized(item.vector, dimension))
            scored.append({"id": item.id, "kind": item.fact.kind, "summary": item.fact.summary,
                           "status": item.status, "score": score})
    scored.sort(key=lambda row: (-float(row["score"]), str(row["id"])))
    return {"items": scored[:request.limit]}
