"""Scoped source retraction without deleting transcript, facts or evidence."""
from __future__ import annotations

from collections import defaultdict, deque
import json
from collections.abc import Sequence

from .store import Store, encode


def _references(raw: str) -> tuple[str, ...]:
    value = json.loads(raw)
    if not isinstance(value, list) or not value or any(not isinstance(ref, str) or not ref for ref in value):
        raise ValueError("Invalid stored source evidence")
    return tuple(value)


def retract(store: Store, graph: str, key: str, fingerprint: str, message_ids: Sequence[str],
            actor_message_id: str, reason: str) -> dict[str, object]:
    """Commit retractions, status changes and one replay receipt atomically."""
    targets = tuple(dict.fromkeys(message_ids))
    if not targets or len(targets) != len(message_ids):
        raise ValueError("Source retraction requires distinct message IDs")
    with store.connection(write=True) as db:
        db.execute("BEGIN IMMEDIATE")
        saved = db.execute("SELECT fingerprint,value FROM receipts WHERE key=?", (key,)).fetchone()
        if saved is not None:
            if saved[0] != fingerprint:
                raise ValueError("Source retraction request ID reused for different arguments")
            return json.loads(saved[1])

        before = {row[0] for row in db.execute(
            "SELECT message_id FROM source_retractions WHERE graph=?", (graph,))}
        actors = {row[0] for row in db.execute(
            "SELECT actor_message_id FROM source_retractions WHERE graph=?", (graph,))}
        if set(targets) & actors:
            raise ValueError("A committed source-retraction request cannot itself be retracted")
        revoked = before | set(targets)
        new = set(targets) - before
        rows = {row["id"]: row for row in db.execute("SELECT * FROM memory_items WHERE graph=?", (graph,))}
        statuses = {identity: row["status"] for identity, row in rows.items()}
        sources = {row[0]: row[1] for row in db.execute("SELECT source_key,source FROM learning")}
        origins: dict[str, list[tuple[tuple[str, ...], tuple[str, ...]]]] = defaultdict(list)
        for row in db.execute(
            "SELECT o.memory_id,o.evidence_ids,o.source_key FROM origins o JOIN memory_items m ON m.id=o.memory_id "
            "WHERE m.graph=? ORDER BY o.rowid", (graph,)):
            if row[2] not in sources:
                raise ValueError("Memory origin has no learning source")
            origins[row[0]].append((_references(row[1]), store._source_users(sources[row[2]])))
        changes: dict[str, list[tuple[str, str, tuple[str, ...], tuple[str, ...]]]] = defaultdict(list)
        for row in db.execute(
            "SELECT c.memory_id,c.event_key,c.action,c.evidence FROM changes c "
            "JOIN memory_items m ON m.id=c.memory_id WHERE m.graph=?", (graph,)):
            if row[2] in {"forget", "supersede"}:
                refs = _references(row[3])
                source_key, separator, _ = row[1].rpartition(":retract:")
                if row[2] == "supersede" and (not separator or source_key not in sources):
                    raise ValueError("Memory supersession has no learning source")
                context = store._source_users(sources[source_key]) if row[2] == "supersede" else refs
                changes[row[0]].append((row[1], row[2], refs, context))
        successors: dict[str, list[tuple[str, str]]] = defaultdict(list)
        predecessors: dict[str, list[str]] = defaultdict(list)
        for row in db.execute(
            "SELECT r.previous_id,r.current_id,r.source_key FROM replacements r "
            "JOIN memory_items p ON p.id=r.previous_id JOIN memory_items n ON n.id=r.current_id "
            "WHERE p.graph=? AND n.graph=?", (graph, graph)):
            successors[row[0]].append((row[1], row[2]))
            predecessors[row[1]].append(row[0])

        for identity in sorted(new):
            db.execute("INSERT INTO source_retractions VALUES (?,?,?,?,?)",
                       (graph, identity, key, actor_message_id, reason))

        def live_origins(identity: str) -> list[tuple[str, ...]]:
            return [refs for refs, context in origins[identity] if not (set(refs) | set(context)) & revoked]

        def has_live_forget(identity: str) -> bool:
            return any(action == "forget" and not (set(refs) | set(context)) & revoked
                       for _, action, refs, context in changes[identity])

        def descendants(identity: str) -> set[str]:
            found: set[str] = set()
            pending = [item for item, _ in successors[identity]]
            while pending:
                item = pending.pop()
                if item == identity:
                    raise ValueError("Cyclic memory replacement history")
                if item in found:
                    continue
                found.add(item)
                pending.extend(child for child, _ in successors[item])
            return found

        def blocked(identity: str) -> bool:
            for event, action, refs, context in changes[identity]:
                if (set(refs) | set(context)) & revoked:
                    continue
                if action == "forget":
                    return True
                source_key, separator, _ = event.rpartition(":retract:")
                linked = [item for item, source in successors[identity]
                          if separator and source == source_key]
                if not linked:
                    return True
                # A live successor or an explicit later forget still governs
                # the earlier fact, including through a replacement chain.
                related = set(linked)
                for item in linked:
                    related.update(descendants(item))
                if any(statuses[item] == "active" or has_live_forget(item) for item in related):
                    return True
            return False

        disabled: set[str] = set()
        retained: set[str] = set()
        affected = {identity for identity, groups in origins.items()
                    if any((set(refs) | set(context)) & new for refs, context in groups)}
        for identity in sorted(affected):
            live = live_origins(identity)
            if statuses[identity] == "active" and not live:
                db.execute("UPDATE memory_items SET status='superseded' WHERE id=?", (identity,))
                statuses[identity] = "superseded"
                disabled.add(identity)
            elif statuses[identity] == "active":
                # Match the learning algorithm: one reinforcement for each
                # surviving origin that adds previously unseen evidence.
                seen: set[str] = set()
                count = 0
                for refs in live:
                    if not set(refs) <= seen:
                        count += 1
                    seen.update(refs)
                previous = int(rows[identity]["reinforcement"])
                if count < previous:
                    db.execute("UPDATE memory_items SET reinforcement=? WHERE id=?",
                               (max(1, count), identity))
                retained.add(identity)

        queue: deque[str] = deque()
        for identity, actions in changes.items():
            if any((set(refs) | set(context)) & new and not (set(refs) | set(context)) & before
                   for _, _, refs, context in actions):
                queue.append(identity)
        for identity in sorted(disabled):
            queue.extend(predecessors[identity])
        restored: set[str] = set()
        visited: set[str] = set()
        while queue:
            identity = queue.popleft()
            if identity in visited:
                continue
            visited.add(identity)
            if statuses[identity] == "active":
                continue
            if blocked(identity):
                continue
            if not live_origins(identity):
                queue.extend(predecessors[identity])
                continue
            if any(statuses[item] == "active" or has_live_forget(item) for item in descendants(identity)):
                continue
            if not changes[identity]:
                raise ValueError("Cannot restore a superseded memory without recorded history")
            db.execute("UPDATE memory_items SET status='active' WHERE id=?", (identity,))
            statuses[identity] = "active"
            restored.add(identity)

        for action, identities in (("source_retract", disabled), ("source_restore", restored)):
            for index, identity in enumerate(sorted(identities)):
                db.execute("INSERT INTO changes VALUES (?,?,?,?,?)",
                           (key + ":" + action + ":" + str(index), identity, action, reason, encode(targets)))
        result: dict[str, object] = {
            "text": "已撤销指定来源的记忆影响；原始对话和历史记录继续保留。",
            "retracted_message_ids": list(targets),
            "newly_retracted": len(new),
            "affected_ids": sorted(disabled),
            "retained_ids": sorted(retained),
            "restored_ids": sorted(restored),
        }
        db.execute("INSERT INTO receipts VALUES (?,?,?)", (key, fingerprint, encode(result)))
        db.commit()
        return result
