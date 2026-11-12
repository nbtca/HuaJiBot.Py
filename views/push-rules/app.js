const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView;
const eventNames = { JoinLeft: "加入/离开", Chat: "聊天", PlayerDeath: "死亡", PlayerAchievement: "进度" };
let snapshot = null;
let busy = false;
let dirty = false;

function status(message, kind = "") {
  $("status").textContent = message;
  $("status").className = kind;
}
function markDirty() {
  dirty = true;
  status("有未保存的修改");
  $("reload").textContent = "放弃修改并重新读取";
}
function setBusy(value) {
  busy = value;
  $("save").disabled = value || !snapshot;
  $("reload").disabled = value;
  $("rules-form").setAttribute("aria-busy", String(value));
  for (const fieldset of $("rules-form").querySelectorAll("fieldset")) fieldset.disabled = value;
}
function splitList(value) {
  return value.split(/[,，\n]/u).map((part) => part.trim()).filter(Boolean);
}
function input(value = "", label = "", target = false) {
  const element = document.createElement("input");
  element.value = value;
  element.setAttribute("aria-label", label);
  if (target) element.setAttribute("list", "sessions");
  return element;
}
function targetsInput(values, label) {
  const element = document.createElement("textarea");
  element.rows = 2;
  element.value = values.join("\n");
  element.setAttribute("aria-label", label);
  return element;
}
function cell(row, element, className = "") {
  const td = document.createElement("td");
  td.className = className;
  td.append(element);
  row.append(td);
  return element;
}
function remove(row, after = () => {}) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "删除";
  button.className = "remove";
  button.addEventListener("click", () => { row.remove(); after(); markDirty(); });
  cell(row, button);
}
function check(value, label) {
  const wrapper = document.createElement("label");
  wrapper.className = "check";
  const element = document.createElement("input");
  element.type = "checkbox";
  element.checked = value;
  wrapper.append(element, document.createTextNode(label));
  return wrapper;
}
function groupNames() {
  return [...$("github-groups").rows].map((row) => row.cells[0].firstElementChild.value).filter(Boolean);
}
function refreshRoutes(renameFrom, renameTo) {
  for (const select of $("github-routes").querySelectorAll("select")) {
    const current = select.value === renameFrom ? renameTo : select.value;
    const names = groupNames();
    select.replaceChildren(new Option("选择分组", ""));
    for (const name of names) select.add(new Option(name, name));
    if (current && !names.includes(current)) select.add(new Option(`${current}（分组不存在）`, current));
    select.value = current || "";
  }
}
function addGroup(name = "", targets = []) {
  const row = document.createElement("tr");
  const field = cell(row, input(name, "GitHub 分组名称"));
  cell(row, targetsInput(targets, "GitHub 分组目标"), "target");
  field.addEventListener("input", () => {
    refreshRoutes(field.dataset.previous || "", field.value);
    field.dataset.previous = field.value;
  });
  field.dataset.previous = name;
  remove(row, refreshRoutes);
  $("github-groups").append(row);
}
function addRoute(repo = "", group = "") {
  const row = document.createElement("tr");
  cell(row, input(repo, "GitHub 仓库全名"));
  const select = cell(row, document.createElement("select"));
  select.setAttribute("aria-label", "GitHub 仓库分组");
  select.add(new Option(group, group));
  remove(row);
  $("github-routes").append(row);
  refreshRoutes();
}
function addCalendar(rule = { session: "", mode: "all", keywords: [] }) {
  const row = document.createElement("tr");
  cell(row, input(rule.session, "日程目标会话", true), "target");
  const select = cell(row, document.createElement("select"));
  select.setAttribute("aria-label", "日程过滤模式");
  for (const [value, label] of [["all", "全部"], ["whitelist", "包含关键词"], ["blacklist", "排除关键词"]]) select.add(new Option(label, value));
  select.value = rule.mode;
  cell(row, input(rule.keywords.join(", "), "日程关键词"));
  remove(row);
  $("calendar-groups").append(row);
}
function addBridgeGroup(body, rule = {}) {
  const row = document.createElement("tr");
  cell(row, input(rule.session || "", "游戏桥目标会话", true), "target");
  cell(row, input(rule.legacy_group_id || "", "旧游戏桥群号"));
  const toggles = document.createElement("div");
  for (const [key, label] of [["enabled", "启用"], ["to_client", "群 → 游戏"], ["from_client", "游戏 → 群"]]) {
    const wrapper = check(rule[key] !== false, label);
    wrapper.firstElementChild.dataset.key = key;
    toggles.append(wrapper);
  }
  cell(row, toggles);
  const events = document.createElement("div");
  for (const [key, label] of Object.entries(eventNames)) {
    const wrapper = check(!(rule.disabled_events || []).includes(key), label);
    wrapper.firstElementChild.dataset.event = key;
    events.append(wrapper);
  }
  cell(row, events);
  remove(row);
  body.append(row);
}
function renderClients(clients, connections) {
  $("bridge-clients").replaceChildren();
  if (!clients.length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "请在插件设置中添加服务器连接，随后在此维护群转发规则。";
    $("bridge-clients").append(empty);
  }
  for (const client of clients) {
    const section = document.createElement("div");
    section.className = "client";
    section.dataset.index = client.index;
    const heading = document.createElement("h3");
    const connected = connections.find((item) => item.index === client.index)?.connected;
    heading.textContent = `服务器 ${client.index + 1} · ${connected ? "已连接" : "未连接"}`;
    const wrap = document.createElement("div");
    wrap.className = "table-wrap";
    const table = document.createElement("table");
    const header = document.createElement("thead");
    const row = document.createElement("tr");
    for (const name of ["目标会话", "旧游戏桥群号", "转发方向", "接收事件", "操作"]) {
      const th = document.createElement("th"); th.textContent = name; row.append(th);
    }
    header.append(row);
    const body = document.createElement("tbody");
    for (const rule of client.groups) addBridgeGroup(body, rule);
    table.append(header, body); wrap.append(table);
    const add = document.createElement("button");
    add.type = "button"; add.textContent = "添加游戏桥目标";
    add.addEventListener("click", () => { addBridgeGroup(body); markDirty(); });
    section.append(heading, wrap, add);
    $("bridge-clients").append(section);
  }
}
function value(id, content) { $(id).value = content; }
function toggle(id, content) { $(id).checked = content; }
function render(data) {
  snapshot = data;
  const rules = data.rules;
  $("github-groups").replaceChildren(); $("github-routes").replaceChildren();
  for (const [name, targets] of Object.entries(rules.github.groups)) addGroup(name, targets);
  for (const [repo, group] of Object.entries(rules.github.routes)) addRoute(repo, group);
  $("calendar-groups").replaceChildren();
  for (const group of rules.calendar.groups) addCalendar(group);
  toggle("calendar-enabled", rules.calendar.enabled);
  toggle("weekly", rules.calendar.club.weekly); toggle("daily", rules.calendar.club.daily);
  value("weekly-hour", rules.calendar.club.weekly_hour); value("weekly-day", rules.calendar.club.weekly_day); value("daily-hour", rules.calendar.club.daily_hour);
  toggle("tickets-enabled", rules.repair.queries_enabled); toggle("repair-enabled", rules.repair.reminder_enabled);
  value("repair-hour", rules.repair.hour); value("repair-since", rules.repair.since);
  for (const key of ["open", "accepted", "committed"]) value(`${key}-days`, rules.repair.threshold_days[key]);
  value("repair-events", rules.repair.event_sessions.join("\n")); value("repair-targets", rules.repair.reminder_sessions.join("\n")); value("repair-raw", rules.repair.raw_session);
  toggle("summary-enabled", rules.summary.enabled); toggle("summary-scheduled", rules.summary.scheduled);
  value("summary-hour", rules.summary.hour); value("summary-minute", rules.summary.minute); value("summary-min", rules.summary.min_messages);
  value("summary-targets", rules.summary.sessions.join("\n"));
  renderClients(rules.bridge.clients, data.status.bridge);
  $("targets").replaceChildren(); $("sessions").replaceChildren();
  for (const origin of data.allowed_sessions) {
    const li = document.createElement("li"); li.textContent = origin; $("targets").append(li);
    $("sessions").append(new Option(origin, origin));
  }
  if (!data.allowed_sessions.length) {
    const li = document.createElement("li"); li.textContent = "尚未添加授权会话。"; $("targets").append(li);
  }
  $("github-status").textContent = `GitHub · ${!data.status.github_enabled ? "未启用" : !data.status.github_configured ? "待配置连接" : data.status.github_connected ? "已连接" : "连接中"}`;
  $("queue-status").textContent = `通知队列 · 待发送 ${data.status.queue.pending || 0} · 失败 ${data.status.queue.failed || 0}`;
  $("repair-status").textContent = data.status.nsq_configured ? "NSQ 事件接收已启用。" : "NSQ 连接可在插件设置中配置。";
  $("rules-form").hidden = false;
  $("reload").textContent = "重新读取";
  dirty = false;
}
function number(id) { return Number($(id).value); }
function dictionary(rows, keyIndex, getValue) {
  const result = Object.create(null);
  for (const row of rows) {
    const key = row.cells[keyIndex].firstElementChild.value.trim();
    if (Object.hasOwn(result, key)) throw new Error(`存在重复条目 ${key}`);
    result[key] = getValue(row);
  }
  return result;
}
function collect() {
  const groups = dictionary($("github-groups").rows, 0, (row) => splitList(row.cells[1].firstElementChild.value));
  const routes = dictionary($("github-routes").rows, 0, (row) => row.cells[1].firstElementChild.value);
  return {
    github: { groups, routes },
    calendar: { enabled: $("calendar-enabled").checked,
      groups: [...$("calendar-groups").rows].map((row) => ({ session: row.cells[0].firstElementChild.value.trim(), mode: row.cells[1].firstElementChild.value, keywords: splitList(row.cells[2].firstElementChild.value) })),
      club: { daily: $("daily").checked, weekly: $("weekly").checked, daily_hour: number("daily-hour"), weekly_hour: number("weekly-hour"), weekly_day: number("weekly-day") } },
    repair: { queries_enabled: $("tickets-enabled").checked, since: $("repair-since").value.trim(), reminder_enabled: $("repair-enabled").checked, hour: number("repair-hour"),
      reminder_sessions: splitList($("repair-targets").value), event_sessions: splitList($("repair-events").value), raw_session: $("repair-raw").value.trim(),
      threshold_days: { open: number("open-days"), accepted: number("accepted-days"), committed: number("committed-days") } },
    bridge: { clients: [...$("bridge-clients").querySelectorAll(".client")].map((section) => ({ index: Number(section.dataset.index), groups: [...section.querySelector("tbody").rows].map((row) => ({
      session: row.cells[0].firstElementChild.value.trim(), legacy_group_id: row.cells[1].firstElementChild.value.trim(),
      enabled: row.querySelector('[data-key="enabled"]').checked, to_client: row.querySelector('[data-key="to_client"]').checked, from_client: row.querySelector('[data-key="from_client"]').checked,
      disabled_events: [...row.querySelectorAll("[data-event]")].filter((element) => !element.checked).map((element) => element.dataset.event),
    })) })) },
    summary: { enabled: $("summary-enabled").checked, scheduled: $("summary-scheduled").checked, sessions: splitList($("summary-targets").value), hour: number("summary-hour"), minute: number("summary-minute"), min_messages: number("summary-min") },
  };
}
async function reload() {
  if (busy) return;
  setBusy(true); status("正在读取…");
  try { render(await bridge.apiGet("rules")); status("已读取当前规则"); }
  catch (error) { status(error.message, "error"); }
  finally { setBusy(false); }
}
$("reload").addEventListener("click", reload);
$("rules-form").addEventListener("input", markDirty);
$("rules-form").addEventListener("change", markDirty);
for (const [id, handler] of [["add-github-group", addGroup], ["add-github-route", addRoute], ["add-calendar", addCalendar]]) $(id).addEventListener("click", () => { handler(); markDirty(); });
$("rules-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy || !snapshot) return;
  let rules;
  try { rules = collect(); } catch (error) { status(error.message, "error"); return; }
  setBusy(true); status("正在保存…");
  try {
    render(await bridge.apiPost("rules/save", { revision: snapshot.revision, rules }));
    status("已保存，后续新通知使用新规则", "success");
  } catch (error) { status(error.message, "error"); }
  finally { setBusy(false); }
});
window.addEventListener("beforeunload", (event) => { if (dirty) { event.preventDefault(); event.returnValue = ""; } });
try {
  if (!bridge) throw new Error("请从 AstrBot 的插件详情中打开推送规则视图");
  const context = await bridge.ready();
  const applyTheme = (value) => { document.documentElement.dataset.theme = value.isDark ? "dark" : "light"; };
  applyTheme(context);
  if (bridge.onContext) bridge.onContext(applyTheme);
  await reload();
} catch (error) { status(error.message, "error"); }
