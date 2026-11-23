"""Scoped context, explicit answer and timeline queries with durable receipts."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Awaitable, Sequence
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from .scopes import MemoryScopes
from plugins.models.contract import ChatModels, ModelError, ModelRequest
from . import classic
from .event_time import label_event_time, parse_event_time
from .io import run_io
from .retrieval import Hit, render, retrieve
from .schema import Kind
from .store import Store, digest

Intent = Literal["context", "answer", "timeline", "interest", "procedure"]
Vectors = Callable[[Sequence[str]], Awaitable[tuple[tuple[float, ...], ...]]]


def hypothesis_prompt(query: str, style: str) -> str:
    # Upstream engine._explicit_hypothesis_prompt, 47e0d7cc (MIT).
    if style == "event":
        return ("你是个人助手的记忆系统。根据用户提问，生成一条带具体时间的假想记忆条目，"
                "格式如 '[2026-03-08] 用户...'\n"
                "规则：第三人称、简洁事实陈述、只输出那一条文本\n\n"
                f"用户提问：{query}\n假想记忆条目：")
    return ("你是个人助手的记忆系统。根据用户提问，生成一条假想记忆条目。\n"
            "规则：始终生成肯定式、第三人称（'用户…'）、简洁事实陈述、只输出那一条文本\n\n"
            f"用户提问：{query}\n假想记忆条目：")


class Queries:
    def __init__(self, store: Store, scopes: MemoryScopes, models: ChatModels,
                 vectors: Vectors, *, max_chars: int, strategy: str,
                 config: classic.RetrievalConfig, model_id: str | None,
                 hyde_enabled: bool, hyde_timeout: float):
        self.store, self.scopes, self.models, self.vectors = store, scopes, models, vectors
        self.max_chars, self.strategy, self.config = max_chars, strategy, config
        self.model_id, self.hyde_enabled, self.hyde_timeout = model_id, hyde_enabled, hyde_timeout
        self.lock = asyncio.Lock()

    async def _hypothesis(self, key: str, query: str, style: str) -> tuple[str, bool]:
        identity = "hypothesis:" + digest([key, style])
        fingerprint = digest([query, style])
        old = await run_io(self.store.receipt, identity, fingerprint)
        if old is not None:
            return str(old["text"]), bool(old["degraded"])
        value, degraded = "", False
        try:
            async with self.models.independent_execution(model_id=self.model_id) as execution:
                model = execution.chat()
                response = await asyncio.wait_for(model.complete(ModelRequest(
                    messages=({"role": "user", "content": hypothesis_prompt(query, style)},),
                    max_output_tokens=80, disable_reasoning=True,
                    # Attempt identity never reuses a failed binding after a restart.
                    request_key="default-memory-hypothesis:" + uuid4().hex,
                )), timeout=self.hyde_timeout)
                value = (response.content or "").strip()[:1600]
        except (RuntimeError, TimeoutError) as error:
            if not isinstance(error, TimeoutError) and not ModelError.matches(error):
                raise
            degraded = True
        saved = await run_io(self.store.save_receipt, identity, fingerprint,
                            {"text": value, "degraded": degraded})
        return str(saved["text"]), bool(saved["degraded"])

    async def recall(self, key: str, session_id: str, query: str, *, limit: int = 8,
                     timestamp: str | None = None, intent: Intent = "context",
                     memory_kind: Kind | None = None, time_filter: str = "") -> dict[str, object]:
        if intent not in {"context", "answer", "timeline", "interest", "procedure"}:
            raise ValueError("Unknown memory query intent")
        if not 1 <= limit <= 200:
            raise ValueError("Memory limit must be between 1 and 200")
        graph = self.scopes.route(session_id).read
        if graph is None:
            raise PermissionError("该会话的记忆召回已关闭")
        # Preserve v1 context receipts; no prior receipt is reinterpreted or overwritten.
        args: list[object] = [session_id, graph, query, limit]
        if intent != "context" or memory_kind is not None or time_filter:
            args.extend([intent, memory_kind, time_filter, timestamp])
        fingerprint = digest(args)
        async with self.lock:
            old = await run_io(self.store.receipt, key, fingerprint)
            if old is not None:
                # Legacy context receipts did not fingerprint their clock. Keep
                # them readable, but reject changed clocks on new receipts.
                prior_time = old.get("queried_at")
                if timestamp and isinstance(prior_time, str) and classic.instant(timestamp) != classic.instant(prior_time):
                    raise ValueError("Memory query identity reused with a different timestamp")
                return old
            now = classic.instant(timestamp) if timestamp else datetime.now(UTC)
            start, end = classic.time_bounds(time_filter, now, timezone=self.config.timezone)
            if intent == "timeline" and start is None:
                raise ValueError("Timeline recall requires time_filter")
            if not self.store.path.exists():
                return {"text": "", "references": [], "status": "Default Memory 尚未初始化"}
            items = await run_io(self.store.items, graph)
            kind = memory_kind or ("event" if intent == "timeline" else None)
            items = classic.filtered(items, kind=kind, start=start, end=end, timezone=self.config.timezone)
            if intent == "interest":
                items = tuple(item for item in items if item.fact.kind in {"preference", "profile"})
            elif intent == "procedure" and memory_kind is None:
                items = tuple(item for item in items if item.fact.kind in {"procedure", "preference"})
            degraded: list[str] = []
            hypotheses: list[str] = []
            vectors: list[tuple[float, ...]] = []
            if intent == "timeline":
                ordered = sorted(items, key=lambda m: (parse_event_time(m.fact.happened_at, timezone=self.config.timezone).start, m.id))
                hits = tuple(Hit(m, 1, 1, "timeline") for m in ordered[-limit:])
            else:
                if intent == "answer" and self.hyde_enabled and self.strategy == "classic":
                    answers = await asyncio.gather(*(self._hypothesis(key, query, style) for style in ("event", "general")))
                    hypotheses = list(dict.fromkeys(value for value, _ in answers if value))
                    if any(failed for _, failed in answers):
                        degraded.append("部分 HyDE 查询不可用，保留原始问题检索")
                queries = list(dict.fromkeys([query, *hypotheses]))
                for text in queries:
                    try:
                        vectors.append((await asyncio.wait_for(self.vectors([text]), timeout=8))[0])
                    except (RuntimeError, TimeoutError) as error:
                        if not isinstance(error, TimeoutError) and not ModelError.matches(error):
                            raise
                        degraded.append("向量查询不可用，保留可用通道和关键词检索")
                if self.strategy == "current":
                    hits = retrieve(items, query, vectors[0] if vectors else None, now, limit=limit)
                else:
                    config = self.config.model_copy(update={"semantic_threshold": 0.35}) if intent == "answer" else self.config
                    hits = classic.retrieve(items, query, vectors, now,
                                            limit=max(limit, 15) if intent == "answer" else limit, config=config)[:limit]
            if intent in {"answer", "timeline", "interest"}:
                rendered, presented = explicit_text(hits, self.max_chars, timezone=self.config.timezone)
            elif self.strategy == "current":
                rendered, presented = render(hits, max_chars=self.max_chars)
            else:
                rendered, presented = classic.render(hits, max_chars=self.max_chars, config=self.config)
            result: dict[str, object] = {
                "text": rendered,
                "references": list(dict.fromkeys(ref for hit in presented for ref in hit.memory.fact.evidence_ids)),
                "memory_ids": [hit.memory.id for hit in hits],
                "presented_ids": [hit.memory.id for hit in presented],
                "scores": [{"id": hit.memory.id, "score": hit.score, "rrf": hit.fusion, "lane": hit.lane} for hit in hits],
                "degraded": "；".join(dict.fromkeys(degraded)) or None,
                "intent": intent, "strategy": self.strategy,
                "hypotheses": hypotheses, "queried_at": now.isoformat(),
            }
            return await run_io(self.store.save_receipt, key, fingerprint, result)


def explicit_text(hits: Sequence[Hit], max_chars: int, *, timezone: str = "Asia/Shanghai") -> tuple[str, tuple[Hit, ...]]:
    text = "长期记忆线索（具体值与新旧状态请核对用户原文；假设查询不是事实）："
    selected = []
    for hit in hits:
        item = hit.memory
        when = "发生于 " + label_event_time(item.fact.happened_at, timezone=timezone) if item.fact.happened_at else "记录于 " + item.created_at
        line = f"\n[{item.id}] ({item.fact.kind}；{when}；来源 {', '.join(item.fact.evidence_ids)}) {item.fact.summary}"
        if item.fact.steps:
            line += "；步骤：" + "；".join(item.fact.steps)
        if item.fact.tool_requirement:
            line += "；用户工具约束：" + item.fact.tool_requirement
        if len(text) + len(line) > max_chars:
            continue
        text += line
        selected.append(hit)
    return (text if selected else ""), tuple(selected)
