import { el, field, select, form, table, number, empty, formula } from "/forms.js";
import { syncView, teamsView, weekView, tradesView, todayView, reviewView, playerCard } from "/views.js";

const fragment = location.hash.slice(1);
if (fragment) { sessionStorage.setItem("inseason-session", fragment); history.replaceState(null, "", "/"); }
const token = sessionStorage.getItem("inseason-session") ?? "";
const context = { data: {}, results: {}, tab: "sync", nbaTeam: null, tradeTeam: null };
const tabs = [["sync", "資料與同步", syncView], ["teams", "球隊與手調", teamsView], ["week", "每週對戰", weekView], ["trades", "交易", tradesView], ["today", "今日", todayView], ["review", "每週回顧", reviewView]];

async function request(path, payload) {
  const options = { headers: { Authorization: `Bearer ${token}` } };
  if (payload !== undefined) { options.method = "POST"; options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(payload); }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error ?? `HTTP ${response.status}`);
  return result;
}
function showError(error) {
  const node = document.querySelector("#error"); node.hidden = false;
  node.textContent = `${error.message ?? error}。資料時間：${context.data.state?.sync.last_success ?? "尚未同步"}`;
}
function open(content) {
  document.querySelector("#dialogContent").replaceChildren(content);
  const dialog = document.querySelector("#dialog");
  if (!dialog.open) dialog.showModal();
}
function close() { document.querySelector("#dialog").close(); }
function render() {
  const data = context.data;
  const available = data.availability?.enabled ?? false;
  document.querySelector("#leagueName").textContent = data.state?.selected.name ?? "連結你的聯盟，開始準備本週對戰。";
  document.querySelector("#freshness").textContent = data.state?.sync.last_success ? `資料截至 ${new Date(data.state.sync.last_success).toLocaleString("zh-TW", { timeZone: data.preferences.timezone })}` : "尚未同步";
  const notice = document.querySelector("#notice");
  const old = data.state?.sync.last_success && Date.now() - new Date(data.state.sync.last_success) > data.preferences.stale_warning_seconds * 1000;
  notice.hidden = available && !old && !data.state?.sync.last_error;
  notice.textContent = available ? data.state?.sync.last_error ?? "資料已久未同步，建議重新同步後再採取行動。" : `${data.availability?.reason ?? "資料未就緒"}。${data.availability?.repair ?? ""}`;
  document.querySelector("#syncButton").disabled = !data.selected;
  document.querySelector("#navigation").replaceChildren(...tabs.map(([id, label]) => el("button", { "aria-current": context.tab === id ? "page" : "false", disabled: !["sync", "teams"].includes(id) && !available, onClick: () => navigate(id) }, label)));
  const selected = tabs.find(([id]) => id === context.tab);
  const view = !["sync", "teams"].includes(context.tab) && !available ? empty(`${data.availability.reason}；${data.availability.repair}`) : selected[2](context);
  document.querySelector("#content").replaceChildren(view);
}
function navigate(tab) { context.tab = tab; render(); }
async function refresh() {
  context.data = await request("/api/bootstrap");
  if (context.data.projection) {
    const ignored = context.data.notes?.ignored ?? {};
    for (const player of context.data.projection.players) {
      player.flags = player.flags.filter(flag => !ignored[flag.id] || ignored[flag.id] < context.data.projection.on);
    }
  }
  render();
}
async function run(action, payload, reload = true) {
  document.querySelector("#error").hidden = true;
  const busy = document.querySelector("#busy"); busy.hidden = false;
  document.querySelector("#phase").textContent = action;
  try {
    const started = await request("/api/action", { action, payload });
    while (true) {
      await new Promise(resolve => setTimeout(resolve, 200));
      const job = await request("/api/job");
      if (job.id !== started.job) throw new Error("工作狀態已變更，請重新整理確認");
      document.querySelector("#progress").value = job.progress;
      if (job.status === "failed") throw new Error(job.error);
      if (job.status === "completed") {
        if (reload) {
          if (["sync", "select", "settings", "sources", "mapping", "preferences", "adjustments", "revoke", "refit-acceptance", "validation"].includes(action)) context.results = {};
          await refresh();
        }
        return job.result;
      }
    }
  } finally { busy.hidden = true; }
}

function edit(player, flag = null) {
  const fields = context.data.parameters.fields;
  const target = flag ? (flag.field === "minutes" ? "minutes" : `rate:${flag.field}`) : "minutes";
  const defaultField = fields.find(f => f.targets.includes(target)) ?? fields.find(f => f.targets.includes("minutes"));
  const choose = select("調整欄位", "field", fields.map(f => [f.id, f.label]), defaultField.id);
  const container = el("div");
  function valueControl() {
    const chosen = fields.find(f => f.id === choose.querySelector("select").value);
    const suggestion = flag?.suggestions?.[chosen.id];
    const value = suggestion?.result ?? (chosen.targets.includes("minutes") ? player.minutes : 1);
    container.replaceChildren(chosen.kind === "status" ? select("狀態", "value", Object.keys(chosen.statuses).map(s => [s, s])) : field("設定值", "value", value, "number", { step: "any", min: chosen.minimum, max: chosen.maximum, required: true }));
    if (suggestion) container.append(formula(suggestion, context.data.formulas));
    else if (flag) container.append(el("p", {class:"muted"}, "這項旗標沒有可直接套用的合法數值，請自行選擇可調欄位與設定值。"));
    if (chosen.kind === "status" && !chosen.only_back_to_back) {
      container.append(el("label", {class:"checklist"}, el("input", {type:"checkbox", name:"schedule_return"}), "另外設定回歸日與回歸後狀態"),
        field("回歸日", "return_on", player.player.return_on ?? "", "date"),
        select("回歸後狀態（持續到季末，可另行撤銷）", "return_status", Object.keys(chosen.statuses).map(s => [s,s])));
    }
  }
  choose.querySelector("select").addEventListener("change", valueControl); valueControl();
  const today = context.data.projection.on;
  const content = form([choose, container, el("div", { class: "form-grid" }, field("生效日", "starts_on", today, "date", { required: true }), field("最後生效日（回歸日的前一天）", "ends_on", today, "date", { required: true })), field("理由", "reason", flag?.reason ?? "", "text", { required: true }), el("label", { class: "checklist" }, el("input", { name: "redistribute", type: "checkbox", checked: true }), "分鐘變更時，一起預覽隊友的分鐘重分配")], async values => {
    const chosen = fields.find(f => f.id === values.get("field"));
    const value = chosen.kind === "status" ? values.get("value") : Number(values.get("value"));
    const common = { field: chosen.id, starts_on: values.get("starts_on"), ends_on: values.get("ends_on"), reason: values.get("reason"), replaces: null };
    let changes = [{ ...common, player_id: player.player.id, value }];
    if (chosen.kind === "status" && values.has("schedule_return")) {
      const returning = values.get("return_on");
      if (!returning || returning <= common.starts_on || returning > context.data.projection_rules.ends_on) throw new Error("回歸日須晚於生效日，且不晚於賽季結束日");
      const previous = new Date(`${returning}T00:00:00Z`); previous.setUTCDate(previous.getUTCDate() - 1);
      changes[0].ends_on = previous.toISOString().slice(0,10);
      changes.push({...common, player_id:player.player.id, value:values.get("return_status"), starts_on:returning, ends_on:context.data.projection_rules.ends_on});
    }
    if (chosen.targets.includes("minutes") && values.has("redistribute")) {
      const allocation = await run("redistribute", { player: player.player.id, minutes: value }, false);
      changes = Object.entries(allocation).map(([player_id, amount]) => ({ ...common, player_id, value: amount }));
    }
    previewAdjustment(changes);
  }, "預覽調整", showError);
  open(el("div", {}, el("h2", {}, `調整 ${player.player.name}`), content));
}

function previewAdjustment(changes) {
  const fields = changes.map((c, index) => field(`${context.data.projection.players.find(p => p.player.id === c.player_id)?.player.name ?? c.player_id} · ${c.starts_on} 至 ${c.ends_on}`, String(index), c.value, typeof c.value === "number" ? "number" : "text", { step: "any", required: true }));
  const node = form(fields, async values => {
    const edited = changes.map((c,index) => ({ ...c, value: typeof c.value === "number" ? Number(values.get(String(index))) : values.get(String(index)) }));
    const payload = { expected_sha256: context.data.ledger_sha256, changes: edited, preview: true };
    const result = await run("adjustments", payload, false);
    const impact = result.before ? table(["項目", "調整前", "調整後"], [["本週分數", number(result.before.score), number(result.after.score)], ...result.before.categories.map((c, i) => [c.label, number(c.home), number(result.after.categories[i].home)])]) : empty("聯盟資料尚未可用，本次僅預覽球員數據。聯盟功能恢復後會讀取同一份調整。");
    const playerImpact = table(["球員", "分鐘前", "分鐘後", "出賽前", "出賽後", "聯盟歸屬"], result.projection.players.filter(p => edited.some(e => e.player_id === p.player.id)).map(p => {
      const before = context.data.projection.players.find(b => b.player.id === p.player.id);
      const owner = context.data.snapshot?.teams.find(t => [...t.players, ...Object.keys(t.injury_players)].includes(p.player.id))?.name ?? (context.data.snapshot ? "自由球員" : "—");
      return [p.player.name, number(before.minutes), number(p.minutes), number(before.probability), number(p.probability), owner];
    }));
    open(el("div", {}, el("h2", {}, "確認本次手調影響"), playerImpact, impact, el("p", {}, `共 ${edited.length} 筆，同組保存、同組撤銷。`), el("button", { class: "primary", onClick: async () => { try { await run("adjustments", { ...payload, preview: false }); context.results = {}; close(); render(); } catch (e) { showError(e); } } }, "保存這組調整")));
  }, "計算影響", showError);
  open(el("div", {}, el("h2", {}, "分鐘重分配／調整明細"), el("p", {}, "可修改每位球員的值；助手不會自動強制球隊分鐘總和。"), node));
}
function ignore(flag) {
  open(form([field("忽略到哪一天", "until", context.data.projection.on, "date", { required: true })], async values => { await run("ignore-flag", { id: flag.id, until: values.get("until") }); close(); }, "暫時忽略", showError));
}

Object.assign(context, { run, refresh, render, navigate, open, close, error: showError, edit, ignore, selectNba: team => { context.nbaTeam = team; render(); }, player: player => open(playerCard(context, player)) });
document.querySelector("#closeDialog").addEventListener("click", close);
document.querySelector("#syncButton").addEventListener("click", () => run("sync", {}).catch(showError));
document.querySelector("#quitButton").addEventListener("click", async () => {
  try { await request("/api/quit", {}); document.querySelector("#content").replaceChildren(empty("助手已結束，可以關閉此分頁。")); document.querySelector("#navigation").replaceChildren(); } catch (error) { showError(error); }
});
if (!token) showError(new Error("請從季賽助手啟動器開啟瀏覽器，取得本次啟動的存取權限"));
else refresh().catch(showError);
