import {el, money, node, action, option, catalogue, forecastWarning, renderProjectionDetail, renderRoom, renderBuyers, renderTable, tableRows, renderPlan, renderComparison} from "/view.js";
import {beginTiming, rendered, measure} from "/timing.js";
import {matches, priceCSV, floorBackup} from "/presentation.js";
import {installEditors} from "/editing.js";
import {renderStreamingResult} from "/streaming.js";

let boot, desk, jobs, selected = null, saving = false, comparing = false, composing = false;
let players = new Map(), watched = new Set(), compareRevision = 0, settingsDirty = false, inspected = null;
let pollTimer, polling = false;
let changingPrices = false, stale = false, legacyWatch = [];
let sensitivityReply = null, sensitivityPending = false;
let streamingReply = null, streamingPending = false;
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
  if (!response.ok) {
    const failure = new Error(result.error ?? `HTTP ${response.status}`);
    failure.status = response.status; throw failure;
  }
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
  const current = !changingPrices && !stale && jobs?.state_sha256 === sha() ? jobs[el("mode").value].result : null;
  return current ? {...current, streaming:streamingReply?.state_sha256 === sha() && streamingReply?.mode === el("mode").value ? streamingReply.streaming : null} : null;
}
function unavailable() {
  if (stale) return "請重新載入草稿";
  return jobs?.state_sha256 === sha() && jobs?.[el("mode").value]?.status === "failed" ? "計算失敗" : "更新中";
}
function invalidateComparison() { compareRevision++; el("comparison").replaceChildren(); }
function status() {
  for (const [stage, label] of [["equal", "加總"], ["fit", "依陣容調整"]]) {
    const job = !changingPrices && jobs?.state_sha256 === sha() ? jobs[stage] : null;
    const text = stale ? "請重新載入草稿" : job?.status === "ready" ? "已更新" : job?.status === "failed" ? `失敗：${job.error}` : "更新中";
    el(`${stage}Status`).textContent = `${label}：${text}${job?.status === "ready" && job.elapsed_ns != null ? ` · ${(job.elapsed_ns/1e9).toFixed(2)} 秒` : ""}`;
  }
  el("retry").hidden = stale || ![jobs?.equal, jobs?.fit].some(j => j?.status === "failed");
  const own = desk.market.market.room.find(t => t.id === desk.state.mine);
  el("compare").disabled = stale || saving || comparing || !selected || !own.slots || jobs?.state_sha256 !== sha() || jobs?.[el("mode").value]?.status !== "ready";
}
function renderNominee() {
  const p = players.get(selected), current = result();
  if (!p) { el("nominee").replaceChildren(node("p", "搜尋球員，或從表格指定本輪。", "muted")); return; }
  const cap = current?.caps.find(c => c.player_id === selected);
  const sold = desk.state.sales.some(s => s.player_id === selected);
  const price = desk.market.market.prices.find(q => q.player_id === selected);
  el("nominee").replaceChildren(node("h2", p.name), node("p", p.positions.join(" / "), "muted"), node("div", sold ? "已成交" : current ? money(cap?.amount) : unavailable(), "cap"), node("p", `公允 ${money(p.fair)} · 預期成交 ${money(price?.expected)}`, "muted"), action("檢視價格原因", () => browse(selected)));
  if (cap?.conditional || cap?.reason) el("nominee").append(node("p", cap.conditional ? "條件式估值：先確認位置與市場報價。" : cap.reason, "warning"));
  if (floorBackup(price,cap,sold,boot.league.minimum_bid)) el("nominee").append(node("p", `${money(boot.league.minimum_bid)} 備案需我方先提名，且無人加價。`, "warning"));
  const warning = forecastWarning(p);
  if (warning) el("nominee").append(node("p", warning, "warning"));
}
function controls() {
  const busy = saving || stale;
  const buyer = desk.market.market.room.find(t => t.id === el("buyer").value);
  el("buyerLimit").textContent = buyer ? `剩 ${buyer.slots} 格 · 最高可付 ${money(buyer.maximum_bid)}` : "請明確選擇買家。";
  let legal = false;
  try { const amount = integer(el("amount").value); legal = buyer && amount <= buyer.maximum_bid && amount >= boot.league.minimum_bid && amount % boot.league.bid_increment === 0; } catch { /* Incomplete user input. */ }
  el("sell").disabled = busy || !legal || !selected || desk.state.sales.some(s => s.player_id === selected);
  el("undo").disabled = busy || !desk.state.sales.length;
  el("buyer").disabled = busy; el("amount").disabled = busy;
  for (const id of ["settings", "export", "import", "legacyWatch", "reset", "exportCSV", "buyerSearch"]) el(id).disabled = busy;
  for (const button of document.querySelectorAll(".edit-sale")) button.disabled = busy;
  status();
}
function render() {
  if (!desk) return;
  const current = result();
  el("sourceNotice").hidden = boot.details != null && boot.teams != null;
  el("sourceNotice").textContent = boot.details == null ? "這份資料沒有來源明細，無法確認哪些球員缺當季預測；請重新建置競標資料。" : "這份舊版資料未含球隊名稱與樣本註記；重新建置競標資料可補齊。";
  renderRoom(desk, players, editors.sale);
  renderTable(desk, players, current, watched, browse, nominate, watch, unavailable(), saving || stale, boot.league.minimum_bid);
  renderPlan(current, players, desk.market.market, nominate, unavailable());
  renderNominee(); controls(); renderStreaming();
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
    clearSaleFields();
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
    node("p", `停損價 ${result() ? money(c?.amount) : unavailable()}`),
    node("p", c?.reason ?? (c?.conditional ? "須先確認位置／市場報價。" : "根據目前預算、可用球員與合法組隊計算。"), "muted"));
  if (p.projected_price == null) d.append(node("p", "Yahoo 報價缺失；未當成底價備案。", "warning"));
  const override = desk.state.overrides.find(o => o.player_id === id);
  if (override) d.append(node("p", `本次覆寫：市場 ${override.market == null ? "沿用來源" : money(override.market)} · 位置 ${override.positions?.join(" / ") ?? "沿用來源"} · ${override.reason}`, "warning"));
  renderSensitivity(d,id);
  renderProjectionDetail(d, p);
  d.append(action("修改市場價／位置", () => editors.override(id)));
  d.append(action("指定為本輪", () => { nominate(id); el("playerDialog").close(); }));
}
function browse(id) {
  inspected = id; renderDetails(id); el("playerDialog").showModal();
}
function renderStreaming() {
  const button = el("streamingCompare"), container = el("streamingResult"), current = result();
  button.disabled = streamingPending || saving || stale || !boot.streaming_candidates || !current?.plan?.players;
  button.textContent = streamingPending ? "串流比較計算中…" : `比較串流格數${boot.streaming_candidates ? `（${boot.streaming_candidates.join("／")}）` : ""}`;
  container.replaceChildren();
  if (!boot.streaming_candidates) { container.append(node("p", "此份資料尚未設定串流格數比較，請使用包含比較設定的模型重建競標輸入。", "muted")); return; }
  if (current?.streaming) renderStreamingResult(container,current.streaming,players);
  else if (streamingReply?.state_sha256 === sha() && streamingReply?.mode === el("mode").value && streamingReply.error) container.append(node("p", streamingReply.error, "error"));
  else container.append(node("p", "依目前模式的組隊方案按需計算；每筆成交後需重新比較。", "muted"));
}
async function inspectStreaming() {
  if (streamingPending || saving || stale) return;
  const state = sha(), mode = el("mode").value;
  streamingPending = true; streamingReply = null; render();
  try {
    const reply = await api("streaming", {state_sha256:state,mode});
    if (reply.state_sha256 === state && sha() === state) streamingReply = reply;
  } catch(e) { if (sha() === state) streamingReply = {state_sha256:state,mode,error:e.message}; }
  finally { streamingPending = false; render(); }
}
function renderSensitivity(container, id) {
  if (el("mode").value !== "fit") return;
  const cap = result()?.caps.find(c => c.player_id === id);
  if (!cap || cap.amount == null || cap.reason) return;
  const button = action(sensitivityPending ? "健康抽樣計算中…" : "計算健康抽樣範圍", () => inspectSensitivity(id));
  button.disabled = sensitivityPending || saving || stale;
  container.append(button);
  if (sensitivityReply?.state !== sha() || sensitivityReply?.player !== id) return;
  if (sensitivityReply.error) { container.append(node("p", sensitivityReply.error, "error")); return; }
  const value = sensitivityReply.value;
  container.append(node("p", value.status === "ready" ? `健康抽樣 ${money(value.low)}–${money(value.high)} · 中央停止價 ${money(value.central)}` : "本輪保留加總估值，未採用健康管理調整；不提供抽樣範圍。"));
  if (value.groups.length) container.append(node("p", `各組：${value.groups.map(money).join("、")}。固定參考陣容、梯度、尺度與採用步長，只改變健康分組均值；範圍含中央值。不是信賴區間，也不涵蓋預測或對手假設的全部誤差。`, "muted"));
}
async function inspectSensitivity(id) {
  if (sensitivityPending || saving || stale) return;
  const state = sha();
  sensitivityPending = true; sensitivityReply = null; renderDetails(id);
  try {
    const reply = await api("sensitivity", {state_sha256:state, player_id:id});
    if (reply.state_sha256 === state && sha() === state) sensitivityReply = {state,player:id,value:reply.sensitivity};
  } catch(e) { if (sha() === state) sensitivityReply = {state,player:id,error:e.message}; }
  finally { sensitivityPending = false; if (el("playerDialog").open) renderDetails(inspected); }
}
async function watch(id) {
  if (saving || stale) return;
  const next = new Set(watched); next.has(id) ? next.delete(id) : next.add(id);
  await save({...desk.state, watch:[...next].sort()}, true);
}
function integer(text) {
  const value = text.normalize("NFKC").trim().replace(/^\$\s*/, "");
  if (!/^[1-9]\d*$/.test(value) || !Number.isSafeInteger(Number(value))) throw new Error("金額請填正整數；不接受小數或千分位。");
  return Number(value);
}
function saveError(message, candidate) {
  const descriptions = new Map([
    ["draft.teams: duplicate names", "隊伍名稱不能重複。"],
    ["draft.teams: names must not be blank", "隊伍名稱不能空白。"],
    ["exceeds legal bid or roster capacity", "超過可付金額、名額已滿，或不符合出價單位。"],
    ["cannot complete starter positions", "此筆成交會導致先發位置無法補齊。"],
    ["unknown player or buyer", "找不到這筆成交的球員或買家。"],
    ["draft.sales.player_id: duplicate values", "同一位球員不能重複成交。"]
  ]);
  for (const entry of candidate?.overrides ?? []) {
    if (message === `draft.overrides.${entry.player_id}.market: outside league bid bounds`) return `${players.get(entry.player_id)?.name ?? entry.player_id}：覆寫報價須介於 ${money(boot.league.minimum_bid)} 與 ${money(boot.league.budget)}。`;
    if (message === `auction.players.${entry.player_id}.positions: empty or duplicate`) return `${players.get(entry.player_id)?.name ?? entry.player_id}：請至少選擇一個位置。`;
  }
  const sales = Array.isArray(candidate?.sales) ? candidate.sales : [];
  const index = sales.findIndex(s => message.startsWith(`draft.sales.${s?.id}:`) || message.startsWith(`draft.sales.${s?.id}.amount:`));
  if (index < 0) return descriptions.get(message) ?? message;
  const sale = sales[index], reason = message.slice(message.indexOf(":") + 1).trim();
  return `第 ${index + 1} 筆成交（${players.get(sale.player_id)?.name ?? sale.player_id}）：${descriptions.get(reason) ?? reason}`;
}
async function save(candidate, metadata = false) {
  if (saving) throw new Error("上一筆仍在保存，請稍候。");
  if (stale) throw new Error("請重新載入已保存的草稿後再操作。");
  beginTiming();
  const previousJobs = jobs;
  const previousOverrides = JSON.stringify(desk.state.overrides);
  saving = true; changingPrices = !metadata;
  if (!metadata) jobs = null;
  invalidateComparison(); render(); error("");
  let saved = false;
  try {
    desk = await api("draft", {expected_sha256: sha(), draft: candidate});
    watched = new Set(desk.state.watch);
    el("saved").textContent = `已保存 · 第 ${desk.state.revision} 版`;
    saved = true;
    if (JSON.stringify(desk.state.overrides) !== previousOverrides) {
      try { boot = await api("bootstrap"); desk = boot.desk; players = catalogue(boot); watched = new Set(desk.state.watch); }
      catch (e) { stale = true; jobs = null; error(`已保存；位置與報價資料載入失敗：${e.message}。請重新整理後繼續。`); }
    }
    if (metadata) {
      try { receiveResults(await api("results")); }
      catch (e) { jobs = failedJobs(e.message); error(`已保存；計算結果載入失敗：${e.message}`); }
    }
    return true;
  } catch (e) {
    if (e.status === 400 || e.status === 403) {
      jobs = previousJobs; error(`未保存：${saveError(e.message, candidate)}`);
    } else {
      stale = true; jobs = null;
      error(`保存未確認：${e.message}。請保留輸入並重新整理確認紀錄，避免重複登錄。`);
    }
    return false;
  } finally { saving = false; changingPrices = false; render(); if (saved) { rendered("market", sha()); poll(); } }
}
function failedJobs(message) {
  const failed = {status:"failed", result:null, error:message};
  return {state_sha256:sha(), equal:failed, fit:failed};
}
function receiveResults(next) {
  if (next.state_sha256 !== sha()) {
    stale = true; jobs = null;
    error("其他視窗已更新草稿，請重新整理以載入已保存紀錄。");
  } else if (JSON.stringify(jobs) !== JSON.stringify(next)) {
    jobs = next;
    for (const stage of ["equal", "fit"]) if (next[stage].status === "ready") rendered(stage, sha());
  } else return;
  render();
}
async function sell(event) {
  event.preventDefault(); if (saving || composing) return;
  try {
    if (!selected || !el("buyer").value) throw new Error("請指定球員與買家。");
    const sale = {id: crypto.randomUUID(), player_id: selected, buyer: el("buyer").value, amount: integer(el("amount").value)};
    const buyer = desk.market.market.room.find(t => t.id === sale.buyer);
    if (sale.amount > buyer.maximum_bid) throw new Error(`成交價超過買家最高可付 ${money(buyer.maximum_bid)}。`);
    if (sale.amount < boot.league.minimum_bid || sale.amount % boot.league.bid_increment) throw new Error("成交價不符合聯盟的最低出價或加價單位。");
    if (await save({...desk.state, sales: [...desk.state.sales, sale]})) {
      // Only clear the submitted fields; no model reply may change this form.
      clearSaleFields(); controls();
    }
  } catch (e) { error(e.message); }
}
async function poll() {
  if (polling) return;
  clearTimeout(pollTimer); polling = true;
  const before = desk ? sha() : null;
  try {
    if (!desk || saving || stale) return;
    const next = await api("results");
    if (saving || sha() !== before) return;
    receiveResults(next);
  } catch (e) {
    if (desk && !saving && sha() === before) {
      jobs = failedJobs(e.message);
      render();
    }
  } finally {
    polling = false;
    const updating = !jobs || [jobs.equal, jobs.fit].some(job => job.status === "updating");
    if (!stale) pollTimer = setTimeout(poll, updating ? 60 : 1000);
  }
}
async function comparison(event) {
  event.preventDefault(); if (comparing || saving) return;
  invalidateComparison(); const revision = compareRevision, state = sha(), player = selected, mode = el("mode").value;
  comparing = true; controls(); const began = performance.now();
  try {
    const price = integer(el("comparePrice").value);
    const value = await api("compare", {state_sha256:state, player_id:player, price, mode});
    if (revision === compareRevision && state === sha() && player === selected && mode === el("mode").value && !saving) {
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
  players = catalogue(boot);
  watched = new Set(desk.state.watch);
  try {
    const stored = JSON.parse(localStorage.getItem(`fba-watch:${desk.state.input_sha256}`) ?? "[]");
    if (Array.isArray(stored)) legacyWatch = stored.filter(id => players.has(id));
    el("legacyWatch").hidden = !legacyWatch.length || watched.size > 0;
    const mode = localStorage.getItem(`fba-mode:${desk.state.draft_id}`);
    if (["equal", "fit"].includes(mode)) el("mode").value = mode;
  } catch (e) { error(`無法讀取瀏覽器偏好：${e.message}`); }
  el("subtitle").textContent = `${boot.season_id} · ${boot.league.teams} 隊 · 本機離線`;
  const l = boot.league;
  el("position").replaceChildren(option("", "全部位置"), ...l.positions.map(p => option(p,p)));
  el("rules").replaceChildren(node("p", `${l.categories.map(c => c.label ?? c.id).join("、")} · 預算 ${money(l.budget)} · 最低出價 ${money(l.minimum_bid)}`), node("p", `先發 ${l.starter_slots.map(s => s.id).join("、")} · 板凳 ${l.bench_slots} · IL ${l.injury_slots.map(s => `${s.label} ${s.count} 格`).join("、")}`), node("p", `交易截止：${l.trade_deadline ?? "不設截止日"} · 季後賽 ${l.playoffs.team_count} 隊`));
  render(); rendered("open", sha()); poll();
}

function clearSaleFields() {
  el("buyer").value = ""; el("buyerSearch").value = ""; el("amount").value = ""; renderBuyers(desk);
}
function download(data, type, name) {
  const url = URL.createObjectURL(new Blob([data], {type}));
  const link = document.createElement("a"); link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
const editors = installEditors({desk:()=>desk,boot:()=>boot,players:()=>players,isBusy:()=>saving||stale,isSaving:()=>saving,save,confirmChange,integer,lastError:()=>el("error").textContent});

el("lookup").addEventListener("input", () => {
  const q = el("lookup").value.trim();
  el("matches").replaceChildren(...(q ? [...players.values()].filter(p => matches(p,q)).slice(0,12).map(p => action(`${p.name} · ${p.positions.join("/")}`, () => nominate(p.id))) : []));
});
el("lookup").addEventListener("keydown", e => { if (e.key === "Enter" && !e.isComposing && !e.repeat) { e.preventDefault(); el("matches").querySelector("button")?.click(); } });
document.addEventListener("keydown", e => {
  if (e.key === "/" && !e.isComposing && !e.ctrlKey && !e.metaKey && !e.altKey && !document.querySelector("dialog[open]") && !e.target.closest("input,textarea,select,[contenteditable]")) { e.preventDefault(); el("lookup").focus(); }
});
el("saleForm").addEventListener("compositionstart", () => { composing = true; });
el("saleForm").addEventListener("compositionend", () => { composing = false; });
el("saleForm").addEventListener("keydown", e => { if (e.key === "Enter" && (e.repeat || e.isComposing || e.target.id !== "amount")) e.preventDefault(); });
el("saleForm").addEventListener("submit", sell);
el("buyer").addEventListener("change", controls);
el("buyerSearch").addEventListener("input", () => { el("buyer").value = ""; renderBuyers(desk); controls(); });
el("amount").addEventListener("input", controls);
el("undo").addEventListener("click", () => save({...desk.state, sales:desk.state.sales.slice(0,-1)}));
for (const id of ["filter", "scope", "position", "sort"]) el(id).addEventListener("input", render);
el("mode").addEventListener("change", () => {
  try { localStorage.setItem(`fba-mode:${desk.state.draft_id}`, el("mode").value); }
  catch (e) { error(`模式偏好無法保存：${e.message}`); }
  invalidateComparison(); render();
});
el("legacyWatch").addEventListener("click", async () => {
  if (await save({...desk.state, watch:[...new Set([...watched, ...legacyWatch])].sort()}, true)) {
    el("legacyWatch").hidden = true;
    try { localStorage.removeItem(`fba-watch:${desk.state.input_sha256}`); }
    catch (e) { error(`追蹤已存入草稿；瀏覽器舊紀錄無法移除：${e.message}`); }
  }
});
el("retry").addEventListener("click", async () => { try { jobs = await api("retry", {state_sha256:sha()}); render(); poll(); } catch (e) { error(e.message); } });
el("compareForm").addEventListener("submit", comparison);
el("comparePrice").addEventListener("input", invalidateComparison);
el("settings").addEventListener("click", openSettings);
el("settingsForm").addEventListener("input", () => { settingsDirty = true; });
el("settingsDialog").addEventListener("cancel", dismissSettings);
el("cancelSettings").addEventListener("click", dismissSettings);
async function applySettings(event) {
  event.preventDefault(); if (saving) return;
  const teams = [...el("teamNames").querySelectorAll("input")].map(i => ({id:i.dataset.team,name:i.value.trim()}));
  if (teams.some(team => !team.name)) {
    el("settingsError").textContent = "隊伍名稱不能空白。"; el("settingsError").hidden = false; return;
  }
  const fields = [...el("settingsForm").elements];
  fields.forEach(field => { field.disabled = true; });
  try {
    if (await save({...desk.state, teams, mine:el("mine").value}, el("mine").value === desk.state.mine)) { settingsDirty = false; el("settingsDialog").close(); el("settings").focus(); }
    else { el("settingsError").textContent = "請保留設定並檢查頁面錯誤。"; el("settingsError").hidden = false; }
  } catch (error) { el("settingsError").textContent = error.message; el("settingsError").hidden = false; }
  finally { fields.forEach(field => { field.disabled = false; }); }
}
el("settingsForm").addEventListener("submit", applySettings);
el("closePlayer").addEventListener("click", () => el("playerDialog").close());
el("streamingCompare").addEventListener("click", inspectStreaming);
el("help").addEventListener("click", () => el("helpDialog").showModal());
el("closeHelp").addEventListener("click", () => el("helpDialog").close());
el("exportCSV").addEventListener("click", () => download(priceCSV(tableRows(desk,players,result(),watched),desk,el("mode").value),"text/csv;charset=utf-8",`prices-${desk.state.revision}-${el("mode").value}.csv`));
el("reset").addEventListener("click", async () => {
  if (saving || stale || !await confirmChange("清空全部成交、追蹤及球員覆寫？我方與隊名保留。建議先匯出備份。")) return;
  if (await save({...desk.state,sales:[],overrides:[],watch:[]})) { selected = null; clearSaleFields(); render(); }
});
el("export").addEventListener("click", () => {
  download(JSON.stringify(desk.state,null,2),"application/json",`draft-${desk.state.revision}.json`);
});
el("import").addEventListener("change", async e => {
  const file = e.target.files[0]; e.target.value = ""; if (!file) return;
  try {
    if (file.size > 2_000_000) throw new Error("備份超過 2 MB。");
    const candidate = JSON.parse(await file.text());
    if (!await confirmChange("以此備份取代目前成交紀錄？設定與資料版本仍由後端檢查。")) return;
    if (await save(candidate)) {
      selected = null; clearSaleFields(); render();
    }
  } catch (e) { error(`匯入失敗：${e.message}`); }
});
start().catch(e => error(`無法開啟競標桌：${e.message}。請使用 serve 顯示的完整網址。`));
