export function mount(host, context) {
  let active = true;
  const title = document.createElement("h2");
  title.textContent = "Watch 值班";
  const refresh = document.createElement("button");
  refresh.type = "button";
  refresh.textContent = "刷新";
  const status = document.createElement("p");
  status.setAttribute("role", "status");
  const list = document.createElement("ol");
  const load = async () => {
    refresh.disabled = true;
    try {
      const data = await context.query("recent", {});
      if (!active) return;
      status.textContent = data.ticks.length
        ? "最近检查：" + data.ticks[0].started + " · " + (data.ticks[0].detail || "尚无完成回执")
        : "尚未检查。请确认主动联系已启用并选定私人目标。";
      list.replaceChildren(...data.events.map(event => {
        const entry = document.createElement("li");
        const route = {wake: "唤醒", inbox: "待看", log: "仅记录"}[event.route];
        entry.textContent = [event.decided_at, route, event.title || event.kind, event.reason, event.outcome].filter(Boolean).join(" · ");
        return entry;
      }));
    } catch (error) {
      if (active) status.textContent = "读取失败：" + error.message;
    } finally {
      if (active) refresh.disabled = false;
    }
  };
  refresh.addEventListener("click", load);
  host.replaceChildren(title, refresh, status, list);
  void load();
  return () => { active = false; refresh.removeEventListener("click", load); };
}

export default {dashboard: {mount}};
