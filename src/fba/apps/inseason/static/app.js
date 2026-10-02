import { el, field, select, form, table, number, empty, formula } from "/forms.js";
import { syncView, teamsView, weekView, tradesView, todayView, reviewView, playerCard } from "/views.js";

const fragment = location.hash.slice(1);
if (fragment && fragment !== "content") { sessionStorage.setItem("inseason-session", fragment); history.replaceState(null, "", location.pathname + location.search); }
const token = sessionStorage.getItem("inseason-session") ?? "";
const context = { data: {}, results: {}, tab: new URLSearchParams(location.search).get("view"), nbaTeam: null, tradeTeam: null };
const tabs = [["today", "今日", todayView, "01"], ["week", "本週對戰", weekView, "02"], ["teams", "我的球隊", teamsView, "03"], ["trades", "交易", tradesView, "04"], ["review", "每週回顧", reviewView, "05"], ["sync", "資料與設定", syncView, "⚙"]];
const actionLabels = { today:"計算今日安排", complete:"更新完成紀錄", adopt:"更新採納紀錄", "trade-search": "搜尋交易", partners: "尋找互補對象", trade: "評估交易", week: "計算每週對戰", recommendations: "搜尋換人建議", preferences: "儲存設定", sync: "同步資料" };

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
function jobFeedback(state, action, error = null) {
  const label = actionLabels[action] ?? "處理資料";
  const reason = error?.message ?? String(error ?? "");
  const message = state === "running" ? `正在${label}，請稍候…` : state === "completed" ? `${label}完成。` : reason.includes("CalculationTimeout") || reason.includes("time budget exceeded") ? `${label}逾時，尚未完成；未產生新的結果。` : `${label}失敗：${reason}`;
  context.jobFeedback = { action, state, message };
  for (const node of document.querySelectorAll("[data-job-actions]")) {
    if (!node.dataset.jobActions.split(",").includes(action)) continue;
    node.textContent = message;
    node.classList.toggle("negative", state === "failed");
    node.setAttribute("aria-busy", String(state === "running"));
  }
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
  if (!tabs.some(([id]) => id === context.tab)) context.tab = available ? "today" : "sync";
  document.querySelector("#leagueName").textContent = data.state?.selected.name ?? "連結你的聯盟，開始準備本週對戰。";
  document.querySelector("#freshness").textContent = data.state?.sync.last_success ? `資料截至 ${new Date(data.state.sync.last_success).toLocaleString("zh-TW", { timeZone: data.preferences.timezone })}` : "尚未同步";
  const notice = document.querySelector("#notice");
  const old = data.state?.sync.last_success && Date.now() - new Date(data.state.sync.last_success) > data.preferences.stale_warning_seconds * 1000;
  notice.hidden = available && !old && !data.state?.sync.last_error && !data.state?.sync.forecast_error;
  notice.textContent = available ? data.state?.sync.forecast_error ?? data.state?.sync.last_error ?? "資料已久未同步，建議重新同步後再採取行動。" : `${data.availability?.reason ?? "資料未就緒"}。${data.availability?.repair ?? ""}`;
  document.querySelector("#syncButton").disabled = !data.selected;
  document.querySelector("#navigation").replaceChildren(...tabs.map(([id, label, , mark]) => el("button", { "aria-current": context.tab === id ? "page" : "false", disabled: !["sync", "teams"].includes(id) && !available, onClick: () => navigate(id) }, el("span", {class:"nav-mark", "aria-hidden":"true"}, mark), label)));
  const selected = tabs.find(([id]) => id === context.tab);
  document.querySelector("#pageTitle").textContent = selected[1];
  document.querySelector("#pageEyebrow").textContent = {today:"YOUR DAILY GAME PLAN", week:"THE WEEK AHEAD", teams:"PLAYER INTELLIGENCE", trades:"BUILD A BETTER TEAM", review:"LOOK BACK, MOVE FORWARD", sync:"YOUR WORKSPACE"}[context.tab];
  const view = !["sync", "teams"].includes(context.tab) && !available ? empty(`${data.availability.reason}；${data.availability.repair}`) : selected[2](context);
  document.querySelector("#content").replaceChildren(view);
}
function navigate(tab) {
  context.tab = tab;
  const url = new URL(location.href); url.searchParams.set("view", tab);
  history.replaceState(null, "", url.pathname + url.search);
  render();
  document.querySelector("#content").focus({preventScroll:true});
  window.scrollTo({top:0,behavior:"instant"});
}
async function refresh(redraw = true) {
  context.data = await request("/api/bootstrap");
  if (context.data.projection) {
    const ignored = context.data.notes?.ignored ?? {};
    for (const player of context.data.projection.players) {
      player.flags = player.flags.filter(flag => !ignored[flag.id] || ignored[flag.id] < context.data.projection.on);
    }
  }
  if (redraw) render();
}
async function run(action, payload, reload = true) {
  if (context.pending) throw new Error("已有計算進行中，請等待完成後再操作。");
  context.pending = true;
  let refreshed = false;
  const controls = [...document.querySelectorAll("#content button, #syncButton")].map(node => [node, node.disabled]);
  for (const [node] of controls) node.disabled = true;
  document.querySelector("#error").hidden = true;
  const busy = document.querySelector("#busy"); busy.hidden = false;
  document.querySelector("#phase").textContent = actionLabels[action] ?? "處理資料";
  document.querySelector("#progress").removeAttribute("value");
  jobFeedback("running", action);
  try {
    const started = await request("/api/action", { action, payload });
    while (true) {
      await new Promise(resolve => setTimeout(resolve, 200));
      const job = await request("/api/job");
      if (job.id !== started.job) throw new Error("工作狀態已變更，請重新整理確認");
      if (job.progress > 0) document.querySelector("#progress").value = job.progress;
      if (job.status === "failed") throw new Error(job.error);
      if (job.status === "completed") {
        jobFeedback("completed", action);
        if (reload) {
          if (action === "preferences") context.tradeValueRatio = null;
          if (["sync", "select", "settings", "sources", "mapping", "preferences", "adjustments", "revoke", "refit-acceptance", "validation"].includes(action)) context.results = {};
          await refresh(false);
          refreshed = true;
        }
        return job.result;
      }
    }
  } catch (error) {
    jobFeedback("failed", action, error);
    throw error;
  } finally {
    context.pending = false;
    for (const [node, disabled] of controls) node.disabled = disabled;
    busy.hidden = true;
    if (refreshed) render();
  }
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
document.querySelector("#skipLink").addEventListener("click", event => { event.preventDefault(); document.querySelector("#content").focus(); });
document.querySelector("#syncButton").addEventListener("click", () => run("sync", {}).catch(showError));
document.querySelector("#quitButton").addEventListener("click", async () => {
  try { await request("/api/quit", {}); document.querySelector("#content").replaceChildren(empty("助手已結束，可以關閉此分頁。")); document.querySelector("#navigation").replaceChildren(); } catch (error) { showError(error); }
});
if (!token) showError(new Error("請從季賽助手啟動器開啟瀏覽器，取得本次啟動的存取權限"));
else refresh().catch(showError);
