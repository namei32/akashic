"""Memory operations use public capabilities and a private fact database."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence, Callable, Awaitable
from datetime import UTC, datetime
from dataclasses import replace
from uuid import uuid4
from plugins.content.contract import Content
from plugins.ledger.contract import Input, Message, MessageCatalog, is_user_input
from plugins.models.contract import ChatModels, Embeddings, ModelError
from plugins.turn_projection.contract import TurnProjection
from .io import run_io
from .learning import collect, extract, check, text
from .retrieval import normalized, render, retrieve
from .schema import Draft, Source, Kind, Fact, Retraction
from . import management, source_retraction
from .api import SourceRetraction
from .rule_schema import build_procedure_rule_schema, procedure_rules_conflict
from .retrieval import cosine
from .classic import RetrievalConfig
from .query import Queries, Intent
from .store import Store, digest
from .scopes import MemoryScopes


class Runtime:
    def __init__(self, store: Store, catalog: MessageCatalog, projection: TurnProjection,
                 content: Content, scopes: MemoryScopes, models: ChatModels, embeddings: Embeddings,
                 *, sources: tuple[str, ...], model_id: str | None = None, max_chars: int = 6000,
                 auto_learn: bool = False, mode: str = "staging",
                 strategy: str = "classic", retrieval: RetrievalConfig | None = None,
                 retrieval_model: str | None = None, hyde_enabled: bool = True, hyde_timeout: float = 3,
                 checkpoint_learning: bool = False):
        self.store = store
        self.catalog = catalog
        self.projection = projection
        self.content = content
        self.scopes = scopes
        self.models = models
        self.embeddings = embeddings
        self.sources = sources
        self.model_id = model_id
        self.max_chars = max_chars
        self.learning_lock = asyncio.Lock()
        self.recall_lock = asyncio.Lock()
        self.errors: dict[str, str] = {}
        self.last_total = 0
        self.auto_learn = auto_learn
        self.mode = mode
        self.last_scan_at: str | None = None
        self.last_source_keys: tuple[str, ...] | None = None
        self.control_lock = asyncio.Lock()
        self.changed = asyncio.Event()
        self.current_source: str | None = None
        self.checkpoint_learning = checkpoint_learning
        self.history_ready: Callable[[], bool] = lambda: False
        self.checkpoint_sources: Callable[[Sequence[Source], Mapping[str, int]], Awaitable[tuple[Source, ...]]] | None = None
        self.queries = Queries(store, scopes, models, self.vectors, max_chars=max_chars,
                               strategy=strategy, config=retrieval or RetrievalConfig(),
                               model_id=retrieval_model, hyde_enabled=hyde_enabled, hyde_timeout=hyde_timeout)

    @property
    def history_lookup_available(self) -> bool:
        return self.history_ready()

    async def vectors(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        descriptor = self.embeddings.describe()
        await run_io(self.store.space, descriptor.identity, descriptor.dimensions, initialize=True)
        async with self.embeddings.bind(model_id=descriptor.model_id) as model:
            if model.descriptor.identity != descriptor.identity:
                raise ValueError("Embedding provider changed while opening")
            result = await model.embed(texts)
        if len(result.vectors) != len(texts):
            raise ValueError("Embedding result count mismatch")
        return tuple(normalized(v, descriptor.dimensions) for v in result.vectors)

    async def sources_snapshot(self) -> tuple[Source, ...]:
        heads = self.catalog.snapshot_heads()
        # Scope calls own a protected provider entry and stay on the host loop.
        routes = {session: self.scopes.route(session) for session in heads}
        class Routes:
            def route(self, session_id: str):
                return routes[session_id]
        turns = await run_io(collect, self.catalog, self.projection, self.content,
                             Routes(), self.sources, heads)
        excluded = await run_io(self.store.forgotten_evidence)
        applied = await run_io(self.store.applied_turn_users, excluded) if excluded else {}
        direct: list[Source] = []
        safe_turns: list[Source] = []

        def fragment(turn: Source, users: tuple[str, ...]) -> Source:
            rows = [row for row in json.loads(turn.text)
                    if row["role"] == "user" and row["message_id"] in users]
            if {row["message_id"] for row in rows} != set(users):
                raise ValueError("Remaining turn inputs do not match their source")
            # The assistant output may repeat retracted material. Keep its ID
            # for compaction coverage but never send that output to extraction.
            return replace(turn,
                key="turn-fragment:" + digest([turn.key, users]),
                message_ids=tuple(ref for ref in turn.message_ids if ref not in excluded),
                user_ids=users, digest=digest(["turn-fragment-v1", turn.digest, users]),
                text=json.dumps(rows, ensure_ascii=False))

        for turn in turns:
            if not set(turn.user_ids) & excluded:
                direct.append(turn)
                safe_turns.append(turn)
                continue
            remaining = tuple(ref for ref in turn.user_ids if ref not in excluded)
            if not remaining:
                continue
            safe_turns.append(fragment(turn, remaining))
            already: set[str] = set()
            for source_key, users in applied.get((turn.session_id, turn.ending_id), []):
                if source_key.startswith("turn-fragment:") and set(users) <= set(remaining):
                    previous = fragment(turn, users)
                    if previous.key != source_key:
                        raise ValueError("Applied turn fragment identity changed")
                    direct.append(previous)
                    already.update(users)
            pending = tuple(ref for ref in remaining if ref not in already)
            if pending:
                direct.append(fragment(turn, pending))
        checkpoint_turns = tuple(turn for turn in safe_turns if not set(turn.user_ids) & excluded)
        checkpoints = () if self.checkpoint_sources is None else await self.checkpoint_sources(checkpoint_turns, heads)
        return tuple(sorted((*direct, *checkpoints), key=lambda row: (row.timestamp, row.session_id, row.key)))

    async def learn(self, source: Source) -> tuple[str, ...]:
        descriptor = self.embeddings.describe()
        await run_io(self.store.space, descriptor.identity, descriptor.dimensions, initialize=True)
        draft = await run_io(self.store.draft, source)
        if draft is None:
            items = await run_io(self.store.items, source.graph)
            # Only scoped memories enter the extraction prompt.
            cues = "\n".join(row["text"] for row in json.loads(source.text) if row["role"] == "user")
            cue_vector = (await self.vectors([cues]))[0] if items else None
            candidates = tuple(hit.memory for hit in retrieve(items, cues, cue_vector, datetime.fromisoformat(source.timestamp), limit=12))
            epoch = await run_io(self.store.retry_epoch)
            draft, model = await extract(source, candidates, self.models, model_id=self.model_id, retry_epoch=epoch)
            draft = await run_io(self.store.stage, source, draft, model)
        vectors = await self.vectors([fact.summary for fact in draft.facts]) if draft.facts else ()
        return await run_io(self.store.apply, source, draft, vectors)

    async def set_paused(self, paused: bool, *, request_id: str | None = None,
                         expected_version: int | None = None) -> dict[str, object]:
        try:
            async with self.control_lock:
                return await run_io(self.store.set_paused, paused, request_id or uuid4().hex,
                                    expected_version=expected_version)
        finally:
            # A committed resume remains effective even when its acknowledgement
            # is cancelled; the next admission reads the durable control.
            self.changed.set()

    async def retry_failed(self, *, request_id: str, expected_version: int) -> dict[str, object]:
        sources = await self.sources_snapshot()
        try:
            async with self.control_lock:
                receipt = await run_io(self.store.request_retry, request_id, expected_version,
                                       [source.key for source in sources])
                self.errors = await run_io(self.store.failures)
                return receipt
        finally:
            self.changed.set()

    async def has_retry(self) -> bool:
        return bool(await run_io(self.store.retry_sources))

    def scanned(self, sources: Sequence[Source]) -> None:
        self.last_total = len(sources)
        self.last_source_keys = tuple(source.key for source in sources)
        self.last_scan_at = datetime.now(UTC).isoformat()

    async def _admit(self, source: Source | None = None) -> bool:
        # The durable control and admission are ordered independently of the
        # long model call. A pause can return while an admitted turn settles.
        async with self.control_lock:
            control = await run_io(self.store.control)
            if control["paused"]:
                return False
            if source is not None:
                self.current_source = source.key
            return True

    async def consume(self, *, limit: int | None = None, retry: bool = False, retry_only: bool = False) -> dict[str, object]:
        async with self.learning_lock:
            if not await self._admit():
                return await self.status()
            if retry:
                await run_io(self.store.retry_epoch, advance=True)
            self.errors = await run_io(self.store.failures)
            sources = await self.sources_snapshot()
            self.scanned(sources)
            pending_retry = set(await run_io(self.store.retry_sources))
            for key in pending_retry - set(self.last_source_keys or ()):
                await run_io(self.store.note_failure, key, "重试轮次已不符合当前学习条件，未调用模型")
                await run_io(self.store.settle_retry, key)
            self.errors = await run_io(self.store.failures)
            learned = await run_io(self.store.learned)
            attempted = 0
            for source in sources:
                if retry_only and source.key not in pending_retry:
                    continue
                if limit is not None and attempted >= limit:
                    break
                previous = learned.get(source.key)
                if previous is not None and previous[0] != source.digest:
                    raise ValueError("Learned source prefix changed; operator verification required")
                if previous is not None and previous[1] == "applied":
                    if source.key in pending_retry:
                        await run_io(self.store.settle_retry, source.key)
                    continue
                if source.key in self.errors:
                    if source.key in pending_retry:
                        await run_io(self.store.settle_retry, source.key)
                    continue
                if not await self._admit(source):
                    break
                try:
                    attempted += 1
                    await self.learn(source)
                    if source.key in pending_retry:
                        await run_io(self.store.settle_retry, source.key)
                except asyncio.CancelledError:
                    raise
                except (ValueError, RuntimeError, TimeoutError) as error:
                    self.errors[source.key] = str(error)
                    await run_io(self.store.note_failure, source.key, str(error))
                    if source.key in pending_retry:
                        await run_io(self.store.settle_retry, source.key)
                    if ModelError.matches(error):
                        break
                finally:
                    self.current_source = None
            return await self.status()

    async def audit(self) -> dict[str, object]:
        sources = await self.sources_snapshot()
        self.scanned(sources)
        by_session: dict[str, int] = {}
        for source in sources:
            by_session[source.session_id] = by_session.get(source.session_id, 0) + 1
        return {"eligible_turns": len(sources), "sessions": by_session,
                "source_digests": {source.key: source.digest for source in sources}}

    async def status(self) -> dict[str, object]:
        value = await run_io(self.store.status, self.last_source_keys)
        control = await run_io(self.store.control) if value["initialized"] else {"paused": False, "version": 0}
        return {**value, "eligible_turns": sum(key.startswith(("turn:", "turn-fragment:")) for key in self.last_source_keys or ()),
                "eligible_checkpoints": sum(key.startswith("checkpoint:") for key in self.last_source_keys or ()),
                "eligible_sources": self.last_total, "paused": control["paused"],
                "control_version": control["version"], "auto_learn": self.auto_learn,
                "current_source": self.current_source, "mode": self.mode,
                "checkpoint_learning": self.checkpoint_learning,
                "checkpoint_waiting": self.checkpoint_learning and self.checkpoint_sources is None,
                "strategy": self.queries.strategy, "history_lookup_available": self.history_lookup_available,
                "last_scan_at": self.last_scan_at, "failures": value.get("failures", {})}

    def route(self, session_id: str, *, write: bool = False) -> str:
        route = self.scopes.route(session_id)
        selected = route.write if write else route.read
        if selected is None:
            raise PermissionError("该会话的记忆学习或召回已关闭")
        return selected

    async def recall(self, key: str, session_id: str, query: str, *, limit: int = 8,
                     timestamp: str | None = None, intent: Intent = "context",
                     memory_kind: Kind | None = None, time_filter: str = "") -> dict[str, object]:
        return await self.queries.recall(key, session_id, query, limit=limit, timestamp=timestamp,
                                         intent=intent, memory_kind=memory_kind, time_filter=time_filter)

    async def scopes_for_inspection(self) -> dict[str, object]:
        heads = self.catalog.snapshot_heads()
        groups: dict[str, str] = {}
        for session in sorted(heads):
            if self.catalog.reader(session).deleted:
                continue
            graph = self.scopes.route(session).read
            if graph is not None:
                groups.setdefault(graph, session)
        items = []
        for graph, session in sorted(groups.items(), key=lambda pair: (pair[0] != "default", pair[0])):
            reader = self.catalog.reader(session)
            title = await run_io(lambda: reader.title)
            items.append({"session_id": session, "label": "全局共享记忆" if graph == "default" else
                          "独立范围 · " + (title or session)})
        return {"items": items}

    async def browse(self, request: management.Browse) -> dict[str, object]:
        graph = self.route(request.session_id)
        return await run_io(management.browse, self.store, graph, request)

    async def similar(self, request: management.Similar) -> dict[str, object]:
        return await run_io(management.similar, self.store, self.route(request.session_id), request)

    async def inspect(self, session_id: str, memory_id: str) -> dict[str, object]:
        return await run_io(management.inspect, self.store, self.route(session_id), memory_id)

    async def retract_sources(self, request: SourceRetraction) -> dict[str, object]:
        graph = self.route(request.session_id, write=True)
        actor = self.catalog.reader(request.session_id).get(request.user_message_id)
        if (actor is None or not isinstance(actor.body, Input) or not is_user_input(actor)
                or self.content.legacy_post_commit_effect(actor) == "suppress"):
            raise ValueError("Source retraction requires an eligible real user input")
        targets: list[str] = []
        for source in request.sources:
            if self.route(source.session_id, write=True) != graph:
                raise PermissionError("Source is outside the current memory scope")
            message = self.catalog.reader(source.session_id).get(source.message_id)
            if message is None or not isinstance(message.body, Input) or not is_user_input(message):
                raise ValueError("Source retraction requires existing user messages")
            if message.message_id == actor.message_id:
                raise ValueError("The request cannot retract its own authorization")
            targets.append(message.message_id)
        if len(set(targets)) != len(targets):
            raise ValueError("Source retraction requires distinct message IDs")
        key = "source-retraction:" + request.request_id
        fingerprint = digest(request.model_dump(mode="json"))
        try:
            async with self.learning_lock:
                saved = await run_io(self.store.receipt, key, fingerprint)
                if saved is not None:
                    return saved
                if actor.message_id in await run_io(self.store.retracted_evidence):
                    raise ValueError("A retracted message cannot authorize memory changes")
                return await run_io(source_retraction.retract, self.store, graph, key, fingerprint,
                                    targets, actor.message_id, request.reason)
        finally:
            self.changed.set()

    async def correct(self, request: management.Correction) -> dict[str, object]:
        graph = self.route(request.session_id, write=True)
        reader = self.catalog.reader(request.session_id)
        user = reader.get(request.user_message_id)
        if (user is None or not isinstance(user.body, Input) or not is_user_input(user)
                or self.content.legacy_post_commit_effect(user) == "suppress"):
            raise ValueError("Correction requires an eligible real user input")
        key = "correction:" + request.request_id
        fingerprint = digest(request.model_dump(mode="json"))
        async with self.learning_lock:
            old_receipt = await run_io(self.store.receipt, key, fingerprint)
            if old_receipt is not None:
                return old_receipt
            if user.message_id in await run_io(self.store.retracted_evidence):
                raise ValueError("A retracted message cannot authorize memory changes")
            source = Source(key, graph, request.session_id, user.message_id, user.recorded_at.isoformat(),
                            (user.message_id,), (user.message_id,), fingerprint, "")
            ids = await run_io(self.store.applied, source)
            if ids is None:
                draft = await run_io(self.store.draft, source)
                if draft is None:
                    detail = await run_io(management.inspect, self.store, graph, request.memory_id)
                    if detail["status"] != "active" or detail["fact_version"] != request.expected_fact_version:
                        raise ValueError("Memory changed; inspect the current version before correcting")
                    old = Fact.model_validate(detail["fact"])
                    updated = old.model_copy(update={
                        "summary": request.summary, "happened_at": old.happened_at if request.happened_at is None else request.happened_at,
                        "emotional_weight": old.emotional_weight if request.emotional_weight is None else request.emotional_weight,
                        "category": old.category if request.category is None else request.category,
                        "steps": old.steps if request.steps is None else request.steps,
                        "tool_requirement": old.tool_requirement if request.tool_requirement is None else request.tool_requirement,
                        "evidence_ids": (user.message_id,),
                    })
                    fact = Fact.model_validate(updated.model_dump())
                    draft = check(Draft(facts=(fact,), retract=(Retraction(
                        memory_id=request.memory_id, evidence_ids=(user.message_id,),
                        reason=request.reason, replacement_index=0),)), source,
                        await run_io(self.store.items, graph))
                    descriptor = self.embeddings.describe()
                    await run_io(self.store.space, descriptor.identity, descriptor.dimensions, initialize=True)
                    draft = await run_io(self.store.stage, source, draft, "explicit-user-correction")
                vectors = await self.vectors([value.summary for value in draft.facts])
                ids = await run_io(self.store.apply, source, draft, vectors)
            return await run_io(self.store.save_receipt, key, fingerprint, {
                "text": "已保存新版本并保留旧记忆与来源。", "memory_ids": list(ids),
                "previous_ids": [request.memory_id],
            })

    async def merge(self, request: management.Merge) -> dict[str, object]:
        graph = self.route(request.session_id, write=True)
        user = self.catalog.reader(request.session_id).get(request.user_message_id)
        if (user is None or not isinstance(user.body, Input) or not is_user_input(user)
                or self.content.legacy_post_commit_effect(user) == "suppress"):
            raise ValueError("Merge requires an eligible real user input")
        ids = tuple(dict.fromkeys(request.memory_ids))
        if len(ids) != len(request.memory_ids) or set(ids) != set(request.expected_fact_versions):
            raise ValueError("Merge requires distinct IDs and every expected version")
        key, fingerprint = "merge:" + request.request_id, digest(request.model_dump(mode="json"))
        async with self.learning_lock:
            previous = await run_io(self.store.receipt, key, fingerprint)
            if previous is not None:
                return previous
            if user.message_id in await run_io(self.store.retracted_evidence):
                raise ValueError("A retracted message cannot authorize memory changes")
            source = Source(key, graph, request.session_id, user.message_id, user.recorded_at.isoformat(),
                            (user.message_id,), (user.message_id,), fingerprint, "")
            result = await run_io(self.store.applied, source)
            if result is None:
                draft = await run_io(self.store.draft, source)
                if draft is None:
                    active = {item.id: item for item in await run_io(self.store.items, graph)}
                    if any(identity not in active for identity in ids):
                        raise ValueError("Merge target is not active in this scope")
                    memories = [active[identity] for identity in ids]
                    for identity in ids:
                        detail = await run_io(management.inspect, self.store, graph, identity)
                        if detail["fact_version"] != request.expected_fact_versions[identity]:
                            raise ValueError("Merge target changed; inspect it again")
                    facts = [item.fact for item in memories]
                    tool = facts[0].tool_requirement.strip()
                    if not tool or any(fact.kind != "procedure" or fact.tool_requirement.strip() != tool for fact in facts):
                        raise ValueError("Only procedures with the same explicit tool requirement may merge")
                    if any(cosine(memories[0].vector, item.vector) < .70 for item in memories[1:]):
                        raise ValueError("Procedure candidates are not sufficiently related")
                    schemas = [build_procedure_rule_schema(fact.summary, fact.tool_requirement, list(fact.steps)) for fact in facts]
                    if any(procedure_rules_conflict(left, right) for i, left in enumerate(schemas) for right in schemas[i + 1:]):
                        raise ValueError("Conflicting procedure rules cannot merge")
                    evidence = tuple(dict.fromkeys([*(ref for fact in facts for ref in fact.evidence_ids), user.message_id]))
                    combined = Fact(kind="procedure", summary="；".join(dict.fromkeys(fact.summary for fact in facts)),
                                    tool_requirement=tool,
                                    steps=tuple(dict.fromkeys(step for fact in facts for step in fact.steps)),
                                    emotional_weight=max(fact.emotional_weight for fact in facts),
                                    evidence_ids=evidence)
                    # Existing evidence remains attached; old rows remain as superseded versions.
                    draft = Draft(facts=(combined,), retract=tuple(Retraction(
                        memory_id=identity, evidence_ids=(user.message_id,), reason=request.reason,
                        replacement_index=0) for identity in ids))
                    descriptor = self.embeddings.describe()
                    await run_io(self.store.space, descriptor.identity, descriptor.dimensions, initialize=True)
                    draft = await run_io(self.store.stage, source, draft, "explicit-user-procedure-merge")
                vectors = await self.vectors([fact.summary for fact in draft.facts])
                result = await run_io(self.store.apply, source, draft, vectors)
            return await run_io(self.store.save_receipt, key, fingerprint, {
                "text": "已合并兼容流程，旧版本和全部出处继续保留。",
                "memory_ids": list(result), "previous_ids": list(ids),
            })

    async def prepare(self, snapshot: tuple[Message, ...], source: str) -> Mapping[str, object]:
        turns = self.projection.project(snapshot, source)
        if not turns or turns[-1].status != "open":
            return {}
        by_id = {message.message_id: message for message in snapshot}
        inputs = [by_id[key] for key in turns[-1].message_ids
                  if isinstance(by_id[key].body, Input) and is_user_input(by_id[key])]
        if not inputs or not text(inputs[-1]).strip():
            return {}
        newest = inputs[-1]
        if self.scopes.route(newest.session_id).read is None:
            return {}
        result = await self.recall("context:" + newest.message_id, newest.session_id, text(newest),
                                   timestamp=newest.recorded_at.isoformat())
        reminders = []
        if self.history_lookup_available:
            reminders.append({"name": "memory-history-guidance", "priority": 350,
                              "text": "涉及用户历史事实时先参考长期记忆；证据不足可使用 search_messages，具体数值、日期、实体和最新状态可用 fetch_messages 核对原文。区分用户陈述与助手回答；找不到充分证据时说明不确定，不假定每个问题都有答案。工具可用性仍以本轮授权为准。"})
        if result["text"]:
            reminders.append({"name": "memory:" + newest.message_id, "text": result["text"], "priority": 400})
        if result.get("degraded"):
            reminders.append({"name": "status", "text": str(result["degraded"]), "priority": 300})
        references = result["references"]
        if not isinstance(references, (list, tuple)) or any(not isinstance(key, str) for key in references):
            raise ValueError("Corrupt recall references")
        return {"reminders": tuple(reminders),
                "references": tuple({"ref": key, "resolved_ref": key} for key in references)}
