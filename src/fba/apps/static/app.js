import {el, money, node, action, option, renderRoom, renderTable, renderPlan, renderComparison} from "/view.js";
import {beginTiming, rendered, measure} from "/timing.js";

let boot, desk, jobs, selected = null, saving = false, comparing = false, composing = false;
let players = new Map(), watched = new Set(), compareRevision = 0, settingsDirty = false, inspected = null;
let pollTimer, polling = false;
let token = location.hash.slice(1);
try {
  if (token) sessionStorage.setItem("fba-session", token);
  else token = sessionStorage.getItem("fba-session") ?? "";
  if (token && location.hash) history.replaceState(null, "", location.pathname);
} catch { /* The fragment remains usable when browser storage is disabled. */ }

async function api(path, body) {
  const response = await fetch(`/api/${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: {"Authorization": `Bearer ${token}`, ...(body === undefined ? {} : {"Content-Type": "application/json"})},
    ...(body === undefined ? {} : {body: JSON.stringify(body)})
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error ?? `HTTP ${response.status}`);
  return result;
}
function error(message) { el("error").textContent = message; el("error").hidden = !message; }
function confirmChange(message) {
  const dialog = el("confirmDialog");
  if (dialog.open) return Promise.resolve(false);
  el("confirmText").textContent = message; dialog.returnValue = "cancel";
  return new Promise(resolve => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {once:true});
    dialog.showModal();
  });
}
function sha() { return desk.market.state_sha256; }
function result() {
  return !saving && jobs?.state_sha256 === sha() ? jobs[el("mode").value].result : null;
}
function invalidateComparison() { compareRevision++; el("comparison").replaceChildren(); }
function status() {
  for (const [stage, label] of [["equal", "加總"], ["fit", "依陣容調整"]]) {
    const job = !saving && jobs?.state_sha256 === sha() ? jobs[stage] : null;
    const text = job?.status === "ready" ? "已更新" : job?.status === "failed" ? `失敗：${job.error}` : "更新中";
    el(`${stage}Status`).textContent = `${label}：${text}`;
  }
  el("retry").hidden = ![jobs?.equal, jobs?.fit].some(j => j?.status === "failed");
  el("compare").disabled = saving || comparing || !selected || jobs?.state_sha256 !== sha() || jobs?.equal.status !== "ready";
}
function renderNominee() {
  const p = players.get(selected), current = result();
  if (!p) { el("nominee").replaceChildren(node("p", "搜尋球員，或從表格指定本輪。", "muted")); return; }
  const cap = current?.caps.find(c => c.player_id === selected);
  const sold = desk.state.sales.some(s => s.player_id === selected);
  const price = desk.market.market.prices.find(q => q.player_id === selected);
  el("nominee").replaceChildren(node("h2", p.name), node("p", p.positions.join(" / "), "muted"), node("div", sold ? "已成交" : current ? money(cap?.amount) : "更新中", "cap"), node("p", `公允 ${money(p.fair)} · 預期成交 ${money(price?.expected)}`, "muted"), action("檢視價格原因", () => browse(selected)));
  if (cap?.conditional || cap?.reason) el("nominee").append(node("p", cap.conditional ? "條件式估值：先確認位置與市場報價。" : cap.reason, "warning"));
}
function controls() {
  const buyer = desk.market.market.room.find(t => t.id === el("buyer").value);
  el("buyerLimit").textContent = buyer ? `剩 ${buyer.slots} 格 · 最高可付 ${money(buyer.maximum_bid)}` : "請明確選擇買家。";
  el("sell").disabled = saving || !selected || desk.state.sales.some(s => s.player_id === selected);
  el("undo").disabled = saving || !desk.state.sales.length;
  el("buyer").disabled = saving; el("amount").disabled = saving;
  for (const id of ["settings", "export", "import"]) el(id).disabled = saving;
  status();
}
function render() {
  if (!desk) return;
  const current = result();
  renderRoom(desk, players);
  renderTable(desk, players, current, watched, browse, nominate, watch);
  renderPlan(current, players, desk.market.market, nominate);
  renderNominee(); controls();
  if (el("playerDialog").open) renderDetails(inspected);
  const refs = desk.state.config;
  el("identity").replaceChildren(...[
    `快照 ${boot.snapshot_sha256}`, `競標輸入 ${desk.state.input_sha256}`,
    `草稿版本 ${desk.state.revision} · ${sha()}`,
    ...["league", "season", "model"].map(key => `${key} ${refs[key].effective_sha256}`)
  ].map(text => node("div", text)));
}
async function nominate(id) {
  if (saving || id === selected) return;
  if (el("buyer").value || el("amount").value) {
    if (!await confirmChange("切換本輪球員會清除尚未登錄的買家與金額，繼續？")) return;
    el("buyer").value = ""; el("amount").value = "";
  }
  selected = id; invalidateComparison(); renderNominee(); controls();
  el("lookup").value = ""; el("matches").replaceChildren();
}
function renderDetails(id) {
  const p = players.get(id), q = desk.market.market.prices.find(x => x.player_id === id);
  const c = result()?.caps.find(x => x.player_id === id);
  const d = el("playerDetail");
  d.replaceChildren(node("h2", p.name), node("p", `${p.positions.join(" / ")} · ${p.id}`),
    node("p", `公允價 ${money(p.fair)} · Yahoo 預估 ${money(p.projected_price)}`),
    node("p", `預期成交 ${money(q?.expected)} · 贏標成本 ${money(q?.acquisition)}`),
    node("p", `停損價 ${result() ? money(c?.amount) : "更新中"}`),
    node("p", c?.reason ?? (c?.conditional ? "須先確認位置／市場報價。" : "根據目前預算、可用球員與合法組隊計算。"), "muted"));
  if (p.projected_price == null) d.append(node("p", "Yahoo 報價缺失；未當成底價備案。", "warning"));
  d.append(action("指定為本輪", () => { nominate(id); el("playerDialog").close(); }));
}
function browse(id) {
  inspected = id; renderDetails(id); el("playerDialog").showModal();
}
function watch(id) {
  const next = new Set(watched); next.has(id) ? next.delete(id) : next.add(id);
  try { localStorage.setItem(`fba-watch:${desk.state.input_sha256}`, JSON.stringify([...next])); }
  catch (e) { error(`追蹤未保存：${e.message}`); return; }
  watched = next; render();
}
function integer(text) {
  const value = text.normalize("NFKC").trim().replace(/^\$\s*/, "");
  if (!/^[1-9]\d*$/.test(value) || !Number.isSafeInteger(Number(value))) throw new Error("金額請填正整數；不接受小數或千分位。");
  return Number(value);
}
async function save(candidate) {
  if (saving) throw new Error("上一筆仍在保存，請稍候。");
  beginTiming();
  saving = true; jobs = null; invalidateComparison(); render(); error("");
  let saved = false;
  try {
    desk = await api("draft", {expected_sha256: sha(), draft: candidate});
    el("saved").textContent = `已保存 · 第 ${desk.state.revision} 版`;
    saved = true;
    return true;
  } catch (e) {
    error(`保存未確認：${e.message}。請保留輸入並重新整理確認紀錄，避免重複登錄。`);
    return false;
  } finally { saving = false; render(); if (saved) { rendered("market", sha()); poll(); } }
}
async function sell(event) {
  event.preventDefault(); if (saving || composing) return;
  try {
    if (!selected || !el("buyer").value) throw new Error("請指定球員與買家。");
    const sale = {id: crypto.randomUUID(), player_id: selected, buyer: el("buyer").value, amount: integer(el("amount").value)};
    if (await save({...desk.state, sales: [...desk.state.sales, sale]})) {
      // Only clear the submitted fields; no model reply may change this form.
      el("buyer").value = ""; el("amount").value = ""; controls();
    }
  } catch (e) { error(e.message); }
}
async function poll() {
  if (polling) return;
  clearTimeout(pollTimer); polling = true;
  try {
    if (!desk || saving) return;
    const before = sha(), next = await api("results");
    if (saving || sha() !== before) return;
    if (next.state_sha256 !== before) {
      jobs = null; render(); error("其他視窗已更新草稿，請重新整理以載入已保存紀錄。"); return;
    }
    if (JSON.stringify(jobs) !== JSON.stringify(next)) {
      jobs = next; render();
      for (const stage of ["equal", "fit"]) if (next[stage].status === "ready") rendered(stage, sha());
    }
  } catch (e) {
    if (desk) {
      jobs = {state_sha256:sha(), equal:{status:"failed", result:null, error:e.message}, fit:{status:"failed", result:null, error:e.message}};
      render();
    }
  } finally {
    polling = false;
    const updating = !jobs || [jobs.equal, jobs.fit].some(job => job.status === "updating");
    pollTimer = setTimeout(poll, updating ? 60 : 1000);
  }
}
async function comparison(event) {
  event.preventDefault(); if (comparing || saving) return;
  invalidateComparison(); const revision = compareRevision, state = sha(), player = selected;
  comparing = true; controls(); const began = performance.now();
  try {
    const price = integer(el("comparePrice").value);
    const value = await api("compare", {state_sha256:state, player_id:player, price});
    if (revision === compareRevision && state === sha() && player === selected && !saving) {
      renderComparison(value, players, desk);
      requestAnimationFrame(() => requestAnimationFrame(() => {
        if (revision === compareRevision) measure("compare", state, began);
      }));
    }
  } catch (e) { if (revision === compareRevision) el("comparison").replaceChildren(node("p", e.message, "error")); }
  finally { comparing = false; controls(); }
}
function openSettings() {
  el("mine").replaceChildren(...desk.state.teams.map(t => option(t.id, t.name)));
  el("mine").value = desk.state.mine;
  el("teamNames").replaceChildren(...desk.state.teams.map(t => {
    const label = node("label", `隊伍 ${t.id}`), input = document.createElement("input"); input.value = t.name; input.dataset.team = t.id; input.required = true; label.append(input); return label;
  }));
  settingsDirty = false; el("settingsError").hidden = true; el("settingsDialog").showModal();
}
async function dismissSettings(event) {
  event?.preventDefault();
  if (saving || (settingsDirty && !await confirmChange("放棄尚未套用的隊伍設定？"))) return;
  el("settingsDialog").close(); el("settings").focus();
}
async function start() {
  boot = await api("bootstrap"); desk = boot.desk;
  players = new Map(boot.players.map(p => [p.id,p]));
  try {
    const stored = JSON.parse(localStorage.getItem(`fba-watch:${desk.state.input_sha256}`) ?? "[]");
    if (Array.isArray(stored)) watched = new Set(stored.filter(id => players.has(id)));
  } catch (e) { error(`無法讀取追蹤清單：${e.message}`); }
  el("subtitle").textContent = `${boot.season_id} · ${boot.league.teams} 隊 · 本機離線`;
  const l = boot.league;
  el("rules").replaceChildren(node("p", `${l.categories.map(c => c.label ?? c.id).join("、")} · 預算 ${money(l.budget)} · 最低出價 ${money(l.minimum_bid)}`), node("p", `先發 ${l.starter_slots.map(s => s.id).join("、")} · 板凳 ${l.bench_slots} · IL ${l.injury_slots.map(s => `${s.label} ${s.count} 格`).join("、")}`), node("p", `交易截止：${l.trade_deadline ?? "不設截止日"} · 季後賽 ${l.playoffs.team_count} 隊`));
  render(); rendered("open", sha()); poll();
}

el("lookup").addEventListener("input", () => {
  const q = el("lookup").value.trim().toLowerCase();
  el("matches").replaceChildren(...(q ? [...players.values()].filter(p => `${p.name} ${p.id}`.toLowerCase().includes(q)).slice(0,12).map(p => action(`${p.name} · ${p.positions.join("/")}`, () => nominate(p.id))) : []));
});
el("saleForm").addEventListener("compositionstart", () => { composing = true; });
el("saleForm").addEventListener("compositionend", () => { composing = false; });
el("saleForm").addEventListener("keydown", e => { if (e.key === "Enter" && (e.repeat || e.isComposing || e.target.id !== "amount")) e.preventDefault(); });
el("saleForm").addEventListener("submit", sell);
el("buyer").addEventListener("change", controls);
el("undo").addEventListener("click", () => save({...desk.state, sales:desk.state.sales.slice(0,-1)}));
for (const id of ["filter", "scope"]) el(id).addEventListener("input", render);
el("mode").addEventListener("change", () => { invalidateComparison(); render(); });
el("retry").addEventListener("click", async () => { try { jobs = await api("retry", {state_sha256:sha()}); render(); poll(); } catch (e) { error(e.message); } });
el("compareForm").addEventListener("submit", comparison);
el("comparePrice").addEventListener("input", invalidateComparison);
el("settings").addEventListener("click", openSettings);
el("settingsForm").addEventListener("input", () => { settingsDirty = true; });
el("settingsDialog").addEventListener("cancel", dismissSettings);
el("cancelSettings").addEventListener("click", dismissSettings);
el("settingsForm").addEventListener("submit", async e => {
  e.preventDefault(); if (saving) return;
  const teams = [...el("teamNames").querySelectorAll("input")].map(i => ({id:i.dataset.team,name:i.value.trim()}));
  if (await save({...desk.state, teams, mine:el("mine").value})) { settingsDirty = false; el("settingsDialog").close(); el("settings").focus(); }
  else { el("settingsError").textContent = "未保存；請保留設定並檢查頁面錯誤。"; el("settingsError").hidden = false; }
});
el("closePlayer").addEventListener("click", () => el("playerDialog").close());
el("export").addEventListener("click", () => {
  const url = URL.createObjectURL(new Blob([JSON.stringify(desk.state,null,2)], {type:"application/json"}));
  const link = document.createElement("a"); link.href = url; link.download = `draft-${desk.state.revision}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
el("import").addEventListener("change", async e => {
  const file = e.target.files[0]; e.target.value = ""; if (!file) return;
  try {
    if (file.size > 2_000_000) throw new Error("備份超過 2 MB。");
    const candidate = JSON.parse(await file.text());
    if (!await confirmChange("以此備份取代目前成交紀錄？設定與資料版本仍由後端檢查。")) return;
    if (await save(candidate)) {
      const updated = await api("bootstrap"); boot = updated; desk = updated.desk; players = new Map(updated.players.map(p => [p.id,p]));
      selected = null; el("buyer").value = ""; el("amount").value = ""; render();
    }
  } catch (e) { error(`匯入失敗：${e.message}`); }
});
start().catch(e => error(`無法開啟競標桌：${e.message}。請使用 serve 顯示的完整網址。`));
