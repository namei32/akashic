import { useEffect, useRef, useState } from "react";
import type { WebHostContextV1 } from "@akashic/web-ui-v1";
import { request } from "../../../../packages/akashic-plugin-kit/src/configuration";

type Fact = {kind: string; summary: string; happened_at: string; steps: string[]; tool_requirement: string; evidence_ids: string[]};
type Item = {id: string; fact: Fact; status: string; reinforcement: number; created_at: string; updated_at: string; has_embedding: boolean};
type Page = {items: Item[]; total: number; offset: number; limit: number};
type Detail = Item & {
  origins: {source_key: string; evidence_ids: string[]; retracted: boolean}[];
  changes: {action: string; reason: string}[];
  replacements: {previous_id: string; current_id: string; reason: string}[];
};
const endpoint = "/api/dashboard/default-memory";
const kinds: Record<string, string> = {profile: "资料", preference: "偏好", procedure: "流程", event: "事件"};

export function MemoryBrowser({ctx}: {ctx: WebHostContextV1}) {
  const [scopes, setScopes] = useState<{session_id: string; label: string}[]>([]);
  const [session, setSession] = useState("");
  const [scopesLoading, setScopesLoading] = useState(true);
  const [text, setText] = useState(""), [query, setQuery] = useState("");
  const [sourceText, setSourceText] = useState(""), [sourceRef, setSourceRef] = useState("");
  const [kind, setKind] = useState(""), [status, setStatus] = useState("active");
  const [embedding, setEmbedding] = useState(""), [sortBy, setSortBy] = useState("updated_at");
  const [sortOrder, setSortOrder] = useState("desc");
  const [offset, setOffset] = useState(0), [refresh, setRefresh] = useState(0);
  const [page, setPage] = useState<Page | null>(null);
  const [details, setDetails] = useState<Record<string, Detail>>({});
  const [error, setError] = useState(""), [loading, setLoading] = useState(false);
  const serial = useRef(0), alive = useRef(true), selectedSession = useRef(session);
  selectedSession.current = session;
  useEffect(() => {
    alive.current = true;
    let active = true;
    setScopesLoading(true);
    void request<{items: typeof scopes}>(ctx, endpoint + "/scopes")
      .then(value => { if (active) { setScopes(value.items); setSession(value.items[0]?.session_id ?? ""); } })
      .catch(reason => { if (active) setError(String(reason)); })
      .finally(() => { if (active) setScopesLoading(false); });
    return () => { active = false; alive.current = false; serial.current++; };
  }, [ctx]);
  useEffect(() => {
    if (!session) return;
    const current = ++serial.current;
    setLoading(true); setError(""); setPage(null); setDetails({});
    const params = new URLSearchParams({session_id: session, query, kind, status, source_ref: sourceRef,
      sort_by: sortBy, sort_order: sortOrder, offset: String(offset), limit: "25"});
    if (embedding) params.set("has_embedding", embedding);
    void request<Page>(ctx, endpoint + "/memories?" + params)
      .then(value => { if (alive.current && current === serial.current) setPage(value); })
      .catch(reason => { if (alive.current && current === serial.current) setError(String(reason)); })
      .finally(() => { if (alive.current && current === serial.current) setLoading(false); });
  }, [ctx, session, query, kind, status, sourceRef, embedding, sortBy, sortOrder, offset, refresh]);

  const inspect = async (id: string) => {
    if (details[id]) return;
    const currentSession = session, currentSerial = serial.current;
    try {
      const value = await request<Detail>(ctx, endpoint + "/memories/" + encodeURIComponent(id) +
        "?session_id=" + encodeURIComponent(session));
      if (alive.current && selectedSession.current === currentSession && serial.current === currentSerial)
        setDetails(previous => ({...previous, [id]: value}));
    } catch (reason) { if (alive.current && serial.current === currentSerial) setError(String(reason)); }
  };

  return <section className="memory-browser" aria-label="记忆内容">
    <h2>记忆内容与来源</h2>
    <p>按范围查看提炼结果和版本记录。这里的筛选只读取数据库，不调用模型。</p>
    <form className="memory-filters" onSubmit={event => { event.preventDefault(); setQuery(text); setSourceRef(sourceText); setOffset(0); setRefresh(v => v + 1); }}>
      <label>记忆范围<select value={session} onChange={event => {setSession(event.target.value); setOffset(0);}}>
        {scopes.map(scope => <option key={scope.session_id} value={scope.session_id}>{scope.label}</option>)}
      </select></label>
      <label>类型<select value={kind} onChange={event => {setKind(event.target.value); setOffset(0);}}>
        <option value="">全部类型</option>{Object.entries(kinds).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select></label>
      <label>状态<select value={status} onChange={event => {setStatus(event.target.value); setOffset(0);}}>
        <option value="active">有效</option><option value="superseded">已失效</option><option value="all">全部状态</option>
      </select></label>
      <label>向量状态<select value={embedding} onChange={event => {setEmbedding(event.target.value); setOffset(0);}}>
        <option value="">全部</option><option value="true">已生成</option><option value="false">未生成</option>
      </select></label>
      <label>排序依据<select value={sortBy} onChange={event => {setSortBy(event.target.value); setOffset(0);}}>
        <option value="updated_at">最近更新</option><option value="created_at">创建时间</option>
        <option value="happened_at">事件时间</option><option value="reinforcement">强化次数</option>
        <option value="emotional_weight">重要程度</option><option value="kind">类型</option>
      </select></label>
      <label>排序方向<select value={sortOrder} onChange={event => {setSortOrder(event.target.value); setOffset(0);}}>
        <option value="desc">由大到小</option><option value="asc">由小到大</option>
      </select></label>
      <label className="memory-search">内容或记忆标识<input value={text} onChange={event => setText(event.target.value)} placeholder="查找已保存的内容或标识" /></label>
      <label className="memory-search">来源标识<input value={sourceText} onChange={event => setSourceText(event.target.value)} placeholder="输入来源或消息标识" /></label>
      <button type="submit" disabled={!session || loading}>查找 / 刷新</button>
    </form>
    {error && <p className="memory-error" role="alert">{error}</p>}
    {loading && <p role="status">正在读取记忆…</p>}
    {scopesLoading && <p role="status">正在读取记忆范围…</p>}
    {!scopesLoading && !session && !error && <p>暂无可查看的会话范围。</p>}
    {page && <>
      <p>共 {page.total} 条{page.items.length ? `，显示第 ${offset + 1}–${offset + page.items.length} 条` : "，当前筛选没有结果"}。</p>
      <ul className="memory-records">
        {page.items.map(item => <li key={item.id}>
          <p className="memory-record-meta">{kinds[item.fact.kind] ?? item.fact.kind} · {item.status === "active" ? "有效" : "已失效"} · 强化 {item.reinforcement} 次 · {item.has_embedding ? "向量已生成" : "向量未生成"}</p>
          <p>{item.fact.summary}</p>
          {item.fact.happened_at && <p>事件时间：{item.fact.happened_at}</p>}
          {item.fact.tool_requirement && <p>工具约束：{item.fact.tool_requirement}</p>}
          {item.fact.steps.length > 0 && <ol>{item.fact.steps.map((step, i) => <li key={i}>{step}</li>)}</ol>}
          <details onToggle={event => {if (event.currentTarget.open) void inspect(item.id);}}>
            <summary>来源与版本</summary>
            {!details[item.id] ? <p>正在读取出处…</p> : <>
              <p>来源记录 {details[item.id].origins.length} 条 · 变更记录 {details[item.id].changes.length} 条</p>
              {details[item.id].changes.map((change, i) => <p key={i}>{change.reason}</p>)}
              {details[item.id].replacements.map((change, i) => <p key={i}>版本关联：{change.reason}</p>)}
              <p>创建：{new Date(item.created_at).toLocaleString()} · 最近强化：{new Date(item.updated_at).toLocaleString()}</p>
              <details><summary>查看引用标识</summary><code>{item.id}</code>
                {details[item.id].origins.map((origin, i) => <code key={i}>{origin.evidence_ids.join(" · ")}{origin.retracted ? "（已撤销）" : ""}</code>)}
              </details>
            </>}
          </details>
        </li>)}
      </ul>
      <div className="memory-actions">
        <button type="button" disabled={loading || offset === 0} onClick={() => setOffset(v => Math.max(0, v - 25))}>上一页</button>
        <button type="button" disabled={loading || offset + page.items.length >= page.total} onClick={() => setOffset(v => v + 25)}>下一页</button>
      </div>
    </>}
  </section>;
}
