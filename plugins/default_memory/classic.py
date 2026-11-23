"""Retrieval semantics adapted from upstream 47e0d7cc (MIT, kachofugetsu09).

Sources: memory2/retriever.py and MemoryStore2._score_embedding_rows.
Storage, permissions, clocks and model calls remain owned by the modern plugin.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo
import math
import re

from pydantic import BaseModel, ConfigDict, Field

from .event_time import label_event_time, overlaps, parse_event_time
from .retrieval import Hit, cosine
from .schema import Kind, Memory


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    semantic_threshold: float = Field(default=0.45, ge=0, le=1)
    procedure_threshold: float = Field(default=0.66, ge=0, le=1)
    preference_threshold: float = Field(default=0.5, ge=0, le=1)
    profile_threshold: float = Field(default=0.5, ge=0, le=1)
    event_threshold: float = Field(default=0.5, ge=0, le=1)
    procedure_guard_enabled: bool = True
    high_inject_delta: float = Field(default=0.15, ge=0, le=1)
    hotness_alpha: float = Field(default=0.2, ge=0, le=1)
    half_life_days: float = Field(default=14, gt=0)
    forced_limit: int = Field(default=3, ge=0, le=20)
    norm_limit: int = Field(default=4, ge=0, le=40)
    fact_limit: int = Field(default=4, ge=0, le=200)
    timezone: str = "Asia/Shanghai"


def instant(value: str, *, timezone: str = "Asia/Shanghai") -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=ZoneInfo(timezone)) if parsed.tzinfo is None else parsed


def time_bounds(value: str, now: datetime, *, timezone: str = "Asia/Shanghai") -> tuple[datetime | None, datetime | None]:
    """Date ranges are inclusive days, represented by a half-open interval."""
    if not value.strip():
        return None, None
    zone = ZoneInfo(timezone)
    today = now.astimezone(zone).date()
    value = value.strip()
    if value in {"today", "yesterday"}:
        day = today - timedelta(days=value == "yesterday")
        return datetime.combine(day, time.min, zone), datetime.combine(day + timedelta(days=1), time.min, zone)
    recent = re.fullmatch(r"recent_([1-9][0-9]*)d", value)
    if recent:
        days = int(recent.group(1))
        if days > 36500:
            raise ValueError("Time range exceeds 100 years")
        return now - timedelta(days=days), now
    dates = [part.strip() for part in value.split("~")]
    pattern = r"\d{4}(?:-\d{2}(?:-\d{2})?)?"
    if len(dates) not in {1, 2} or any(not re.fullmatch(pattern, date) for date in dates):
        raise ValueError("Use today/yesterday/recent_Nd or YYYY, YYYY-MM, YYYY-MM-DD ranges")
    start = parse_event_time(dates[0], timezone=timezone).start
    end = parse_event_time(dates[-1], timezone=timezone).end
    if end is not None and start >= end:
        raise ValueError("Time range starts after it ends")
    return start, end


def filtered(items: Sequence[Memory], *, kind: Kind | None = None,
             start: datetime | None = None, end: datetime | None = None,
             timezone: str = "Asia/Shanghai") -> tuple[Memory, ...]:
    selected = []
    for item in items:
        if item.status != "active" or (kind is not None and item.fact.kind != kind):
            continue
        if start is not None or end is not None:
            if not item.fact.happened_at:
                continue
            when = parse_event_time(item.fact.happened_at, timezone=timezone)
            if not overlaps(when, start, end):
                continue
        selected.append(item)
    return tuple(selected)


def hotness(item: Memory, now: datetime, config: RetrievalConfig) -> float:
    age = max(0.0, (now - instant(item.updated_at)).total_seconds() / 86400)
    half_life = config.half_life_days * (1 + item.fact.emotional_weight / 20)
    frequency = (max(0, item.reinforcement) + 1) / (max(0, item.reinforcement) + 2)
    return frequency * math.exp(-math.log(2) * age / half_life)


def retrieve(items: Sequence[Memory], query: str, vectors: Sequence[Sequence[float]],
             now: datetime, *, limit: int = 8, config: RetrievalConfig | None = None) -> tuple[Hit, ...]:
    """Per-query vector top-k -> best vector hit union -> keyword -> weighted RRF."""
    config = config or RetrievalConfig()
    active = {item.id: item for item in items if item.status == "active"}
    dense: dict[str, float] = {}
    for vector in vectors:
        lane: list[tuple[str, float]] = []
        for item in active.values():
            semantic = cosine(vector, item.vector)
            if semantic < config.semantic_threshold:
                continue
            score = round((1 - config.hotness_alpha) * semantic + config.hotness_alpha * hotness(item, now, config), 4)
            lane.append((item.id, score))
        for identity, score in sorted(lane, key=lambda row: (-row[1], row[0]))[:limit]:
            dense[identity] = max(dense.get(identity, -1), score)
    query_terms = extract_terms(query)
    lexical: dict[str, float] = {}
    if query_terms:
        for item in active.values():
            matched = sum(term.lower() in item.fact.summary.lower() for term in query_terms)
            if matched:
                lexical[item.id] = matched / len(query_terms)
    ranked_dense = sorted(dense, key=lambda identity: (-dense[identity], identity))
    ranked_keyword = sorted(lexical, key=lambda identity: (-lexical[identity], -active[identity].reinforcement, identity))
    ranked_keyword = ranked_keyword[:max(30, limit * 2)]
    fused: dict[str, float] = {}
    for ranking, weight in ((ranked_dense, 1.0), (ranked_keyword, 0.5)):
        for rank, identity in enumerate(ranking, 1):
            fused[identity] = fused.get(identity, 0.0) + weight / (60 + rank)
    # Upstream keeps a vector hit's score when the same item also matches keywords.
    score = {identity: dense.get(identity, lexical.get(identity, 0.0)) for identity in fused}
    order = sorted(fused, key=lambda identity: (-fused[identity], -score[identity], identity))
    return tuple(Hit(active[identity], score[identity], fused[identity],
                     "dense+keyword" if identity in dense and identity in lexical else
                     "dense" if identity in dense else "keyword")
                 for identity in order[:limit])


def render(hits: Sequence[Hit], *, max_chars: int = 6000,
           config: RetrievalConfig | None = None) -> tuple[str, tuple[Hit, ...]]:
    """Preserve the three upstream sections, bounded by a real global budget."""
    config = config or RetrievalConfig()
    groups: dict[str, list[tuple[str, Hit]]] = {"forced": [], "norm": [], "fact": []}
    caps = {"forced": config.forced_limit, "norm": config.norm_limit, "fact": config.fact_limit}
    for hit in sorted(hits, key=lambda hit: (-hit.score, hit.memory.id)):
        item = hit.memory
        threshold = getattr(config, item.fact.kind + "_threshold")
        forced = config.procedure_guard_enabled and item.fact.kind == "procedure" and bool(item.fact.tool_requirement)
        if not forced and hit.score < threshold:
            continue
        group = "forced" if forced else "norm" if item.fact.kind in {"procedure", "preference"} else "fact"
        meta = [item.fact.kind]
        if not forced and hit.score < threshold + config.high_inject_delta:
            meta.append("有印象，需核实")
        if item.fact.happened_at:
            meta.append("发生于 " + label_event_time(item.fact.happened_at, timezone=config.timezone))
        else:
            meta.append("记录于 " + item.created_at)
        meta.append("来源 " + ", ".join(item.fact.evidence_ids[:2]))
        line = f"[{item.id}] ({'；'.join(meta)}) {item.fact.summary}"
        if item.fact.tool_requirement:
            line += "；用户工具约束：" + item.fact.tool_requirement
        if item.fact.steps:
            line += "；步骤：" + "；".join(item.fact.steps)
        groups[group].append((line, hit))
    labels = {"forced": "用户流程约束（适用且获授权时遵循）",
              "norm": "用户偏好与流程", "fact": "相关事实与事件"}
    text = "长期记忆（历史资料；不覆盖本轮用户指令，具体细节可回读原文）："
    selected: list[Hit] = []
    for group, candidates in groups.items():
        count = 0
        for line, hit in candidates:
            if count >= caps[group]:
                break
            addition = ("\n" + labels[group] if count == 0 else "") + "\n" + line
            if len(text) + len(addition) > max_chars:
                continue
            text += addition
            selected.append(hit)
            count += 1
    return (text if selected else ""), tuple(selected)


_CJK_STOPWORDS = {
    "用户", "助手", "我们", "他们", "这个", "那个", "什么", "如何", "是否",
    "有没", "没有", "有过", "做过", "进行", "完成", "包括", "通过", "实现",
    "行为", "内容", "相关", "情况", "问题", "方式", "时候", "时间", "目前",
    "当前", "最近", "之前", "以前", "后来", "然后", "因为", "所以", "但是",
    "用户在", "用户对", "的行为吗", "进行了",
}


def extract_terms(query: str) -> list[str]:
    terms: list[str] = []
    ascii_tokens = re.findall(r"[a-zA-Z0-9_\-\.]{2,}", query)
    terms.extend(ascii_tokens)

    cjk_chunks = re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]{2,}", query)
    for chunk in cjk_chunks:
        if len(chunk) <= 4:
            if chunk not in _CJK_STOPWORDS:
                terms.append(chunk)
            continue
        for i in range(len(chunk) - 1):
            bigram = chunk[i:i + 2]
            if bigram not in _CJK_STOPWORDS:
                terms.append(bigram)

    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        if term in seen:
            continue
        seen.add(term)
        result.append(term)
    return result[:20]
