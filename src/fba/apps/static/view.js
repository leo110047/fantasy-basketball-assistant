export const el = (id) => document.getElementById(id);
export const money = (value) => value == null ? "—" : `$${Number(value).toFixed(Number.isInteger(value) ? 0 : 2)}`;
export function node(tag, text, className = "") {
  const n = document.createElement(tag);
  n.textContent = text;
  n.className = className;
  return n;
}
export function action(label, fn, className = "") {
  const b = node("button", label, className);
  b.type = "button";
  b.addEventListener("click", fn);
  return b;
}
export function option(id, name) { const n = node("option", name); n.value = id; return n; }
export const quantity = value => value == null ? "—" : Number(value).toFixed(Number.isInteger(value) ? 0 : 2);
export function catalogue(bootstrap) {
  const details = new Map((bootstrap.details ?? []).map(d => [d.player_id, d]));
  const teams = new Map((bootstrap.teams ?? []).map(t => [t.id,t]));
  const scenarios = (bootstrap.scenarios ?? []).map(s => ({...s,byId:new Map(s.prices.map(p => [p.player_id,p.fair]))}));
  const labels = new Map(bootstrap.league.categories.map(c => [c.id, c.label ?? c.id]));
  return new Map(bootstrap.players.map(p => {
    const detail = details.get(p.id) ?? null;
    const strengths = (detail?.categories ?? []).filter(c => c.z > 0).sort((a,b) => b.z - a.z || a.id.localeCompare(b.id, "en")).slice(0,3).map(c => labels.get(c.id) ?? c.id);
    return [p.id, {...p, detail, strengths, team:teams.get(detail?.team_id) ?? null,
      scenarios:scenarios.map(s => ({name:s.name,fair:s.byId.get(p.id),model:s.model,projection_sha256:s.projection_sha256,calculation_sha256:s.calculation_sha256}))}];
  }));
}
export function forecastWarning(player) {
  if (!player.detail || player.detail.forecast_usable) return "";
  return player.detail.forecast_sources.length ? "當季預測不可用；請核對原因與人工調整。" : "缺當季預測；請核對傷情與人工調整。";
}
function adjustmentText(adjustment) {
  const op = adjustment.operation;
  if (op.kind === "multiply") return `${op.stat_id} × ${quantity(op.factor)}`;
  if (op.kind === "expected_games") return `出賽 ${quantity(op.games)} 場`;
  return `預期回歸 ${op.return_at.split("T")[0]}`;
}
function renderForecastRange(container, player) {
  const range = forecastRange(player);
  if (range.status === "absent") { container.append(node("p", "未提供替代預測情境，無法顯示情境範圍。", "muted")); return; }
  container.append(node("h3", "預測情境比較"));
  container.append(node("p", range.status === "ready" ? `公允價範圍 ${money(range.low)}–${money(range.high)}` : "有情境缺少估值，無法提供完整範圍。"));
  container.append(node("p", "包含目前基準與列出的設定情境；不是信賴區間，也不是停損價的誤差範圍。", "muted"));
  container.append(node("p", `目前基準：${money(player.fair)}`));
  for (const scenario of player.scenarios) {
    const row = node("p", `${scenario.name}：${money(scenario.fair)}`, "detail-sources");
    row.title = `模型 ${scenario.model.input_sha256}\n預測 ${scenario.projection_sha256}\n結果 ${scenario.calculation_sha256}`;
    container.append(row);
  }
}
export function renderProjectionDetail(container, player) {
  const detail = player.detail;
  if (!detail) { container.append(node("p", "此份競標資料沒有預測來源明細，請重新建置後核對。", "warning")); return; }
  const warning = forecastWarning(player);
  if (warning) container.append(node("p", warning, "warning"));
  for (const reason of detail.preparation_warnings) container.append(node("p", reason, "detail-sources warning"));
  container.append(node("h3", "預測與來源"), node("p", `預期 ${quantity(detail.expected_games)} 場 · 每場 ${quantity(detail.minutes)} 分鐘`));
  const stats = node("dl", "", "player-stats");
  for (const stat of detail.stats) {
    const cell = node("div", ""); cell.append(node("dt", stat.id), node("dd", quantity(stat.value))); stats.append(cell);
  }
  if (detail.stats.length) container.append(node("p", "每場數據", "muted"), stats);
  renderForecastRange(container, player);
  if (player.strengths.length) container.append(node("p", `相對強項：${player.strengths.join("、")}`));
  if (player.team) container.append(node("p", `NBA 球隊：${player.team.name ?? player.team.abbreviation}（${player.team.abbreviation}）`));
  if (detail.history_games != null) container.append(node("p", `歷史逐場資料 ${detail.history_games} 場 · 個人樣本門檻 ${detail.history_minimum} 場`, "muted"));
  if (detail.original_expected_games != null && detail.original_expected_games < detail.healthy_games_threshold) container.append(node("p", `出賽風險：校正前預測 ${quantity(detail.original_expected_games)} 場，低於模型健康門檻 ${quantity(detail.healthy_games_threshold)} 場；不是傷病診斷。`, "warning"));
  container.append(node("p", `Yahoo 平均成交 ${money(detail.average_price)}`));
  const sources = detail.forecast_sources.length ? detail.forecast_sources.join("、") : "未提供；請檢查歷史先驗與人工調整";
  container.append(node("p", `當季預測來源：${sources}`, "detail-sources muted"));
  for (const source of detail.forecast_provenance) container.append(node("p", `${source.source_id} · 資料 ${source.available_as_of.split("T")[0]} · ${source.url}`, "detail-sources muted"));
  if (detail.adjustments.length) {
    container.append(node("h3", "人工調整"));
    for (const adjustment of detail.adjustments) container.append(node("p", `${adjustment.assumption ? "假設" : "調整"}：${adjustmentText(adjustment)} · ${adjustment.reason}（${adjustment.source}；公布 ${adjustment.published_at.split("T")[0]}）`, "detail-sources muted"));
  }
}

let roomHash = null;
export function renderRoom(data, players, editSale) {
  if (roomHash === data.market.state_sha256) return;
  roomHash = data.market.state_sha256;
  const {state, market} = data;
  const names = new Map(state.teams.map(t => [t.id, t.name]));
  const own = market.market.room.find(t => t.id === state.mine);
  const cash = market.market.room.reduce((sum,t) => sum+t.budget,0), spendable = market.market.room.filter(t => t.slots).reduce((sum,t) => sum+t.budget,0);
  el("cashSummary").textContent = `全場剩餘 ${money(cash)} · 尚有名額的隊伍可用 ${money(spendable)} · 剩 ${market.market.room.reduce((sum,t) => sum+t.slots,0)} 格`;
  el("teamTitle").textContent = names.get(state.mine);
  el("myBudget").replaceChildren(node("strong", money(own.budget)), node("div", `剩 ${own.slots} 格 · 最高可付 ${money(own.maximum_bid)}`));
  el("room").replaceChildren(...market.market.room.map(t => {
    const d = node("details", "", t.id === state.mine ? "mine" : "");
    d.append(node("summary", `${names.get(t.id)}${t.id === state.mine ? "（我方）" : ""} · ${money(t.budget)} / ${t.slots} 格 · 最高 ${money(t.maximum_bid)}`));
    t.owned.forEach(id => d.append(node("p", `${players.get(id)?.name ?? id} · ${money(state.sales.find(s => s.player_id === id)?.amount)}`)));
    return d;
  }));
  el("history").replaceChildren(...state.sales.map((s,index) => {
    const row = node("li", `${index+1}. ${players.get(s.player_id)?.name ?? s.player_id} → ${names.get(s.buyer)} · ${money(s.amount)} `);
    const button = action("更正", () => editSale(s.id), "edit-sale"); button.setAttribute("aria-label", `更正第 ${index+1} 筆成交`);
    row.append(button); return row;
  }).reverse());
  const last = state.sales.at(-1);
  el("lastSale").textContent = last ? `上一筆：${players.get(last.player_id)?.name ?? last.player_id} · ${names.get(last.buyer)} ${money(last.amount)}` : "尚未成交";
  renderBuyers(data);
}

export function renderBuyers(data) {
  const current = el("buyer").value, query = normalized(el("buyerSearch").value);
  const teams = data.state.teams.filter((t,i) => t.id === current || normalized(`${i+1} ${t.name}`).includes(query));
  el("buyer").replaceChildren(option("", "選擇隊伍"), ...teams.map(t => option(t.id, `${t.name}${t.id === data.state.mine ? "（我方）" : ""}`)));
  el("buyer").value = current;
}

export function tableRows(data, players, result, watched) {
  return priceRows(players, data, result, watched, {query:el("filter").value,scope:el("scope").value,position:el("position").value,sort:el("sort").value});
}

export function renderTable(data, players, result, watched, browse, nominate, watch, unavailable, busy, minimum) {
  const shown = tableRows(data, players, result, watched);
  el("count").textContent = `${shown.length} 位`;
  const body = el("players"), existing = new Map([...body.children].map(row => [row.dataset.player, row]));
  // Remove departed rows first, so a sale does not move every following row.
  const visible = new Set(shown.map(r => r.player.id));
  for (const [id, row] of existing) if (!visible.has(id)) row.remove();
  const rows = shown.map(item => {
    const p = item.player;
    let row = existing.get(p.id);
    if (!row) {
      row = document.createElement("tr"); row.dataset.player = p.id;
      for (let i=0;i<6;i++) row.append(document.createElement("td"));
      row.children[0].append(action("☆", () => watch(p.id)));
      row.children[1].append(action(p.name, () => browse(p.id), "name"), node("small", ""), node("small", "", "warning"), node("small", "", "tags"));
      row.children[3].append(node("span", ""), node("small", ""));
      row.children[4].append(node("span", ""), node("small", ""));
      row.children[4].className = "good";
      row.children[5].append(action("指定", () => nominate(p.id)));
    }
    const cells = row.children, star = cells[0].firstChild, choose = cells[5].firstChild;
    const {quote,cap,sale,difference,discount} = item, held = Boolean(sale);
    text(star, watched.has(p.id) ? "★" : "☆");
    star.disabled = busy;
    star.setAttribute("aria-label", `追蹤 ${p.name}`); star.setAttribute("aria-pressed", String(watched.has(p.id)));
    text(cells[1].children[1], [p.team?.abbreviation, p.positions.join(" / "), p.strengths?.join("、")].filter(Boolean).join(" · "));
    const warning = cells[1].children[2]; text(warning, forecastWarning(p)); warning.hidden = !warning.textContent;
    text(cells[1].children[3], rowTags(item).join(" · "));
    text(cells[2], money(p.fair)); text(cells[3].children[0], held ? "已成交" : money(quote?.expected));
    text(cells[3].children[1], held || difference == null ? "" : `差 ${money(difference)}${discount == null ? "" : ` · ${(discount*100).toFixed(0)}%`}`);
    text(cells[4].children[0], held ? "—" : result ? `${money(cap?.amount)}${cap?.conditional ? " *" : ""}` : unavailable);
    text(cells[4].children[1], floorBackup(quote,cap,held,minimum) ? "底價備案需我方提名" : "");
    cells[4].title = cap?.reason ?? (cap?.conditional ? "需確認位置／報價" : "");
    choose.disabled = held; choose.setAttribute("aria-label", `指定 ${p.name} 為本輪`);
    return row;
  });
  // Keep unchanged elements attached: stable focus, scrolling and native accessibility nodes.
  for (let index=0; index<rows.length; index++) {
    if (body.children[index] !== rows[index]) body.insertBefore(rows[index], body.children[index] ?? null);
  }
  while (body.children.length > rows.length) body.lastChild.remove();
}
function text(element, value) { if (element.textContent !== value) element.textContent = value; }


function renderFitExplanation(fit) {
  const target = el("fitExplanation"), wasOpen = target.firstElementChild?.open;
  target.replaceChildren();
  if (!fit) return;
  const detail = node("details", ""), data = fit.diagnostics;
  detail.open = Boolean(wasOpen);
  detail.append(node("summary", "陣容調整的依據"));
  detail.append(node("p", fit.selected_step === 0 ? "本輪調整未通過改善檢查，停損價保留類別加總估值。" : "停損價使用通過檢查的局部管理邊際估值；步長越大，管理估值的占比越高。"));
  detail.append(node("p", `採用步長 ${fit.selected_step} · 數據樣本 ${fit.samples} · 健康路徑 ${fit.health_samples}。中央指標與每組健康樣本都須通過檢查。${fit.method === "paired_managed_marginal" ? "相同陣容允許持平。" : ""}`, "muted"));
  if (data) {
    detail.append(node("p", `平滑模型指標 ${data.baseline_score.toFixed(4)} → ${data.selected_score.toFixed(4)}`));
    const table = node("table", ""), head = node("tr", ""), body = node("tbody", "");
    for (const title of ["類別", "模擬領先份額", "邊際權重"]) head.append(node("th", title));
    const thead = node("thead", ""); thead.append(head); table.append(thead);
    for (const category of data.categories) {
      const row = node("tr", "");
      for (const value of [category.id, `${(category.lead_share*100).toFixed(1)}%`, quantity(category.marginal_weight)]) row.append(node("td", value));
      body.append(row);
    }
    table.append(body); detail.append(table);
    detail.append(node("p", "領先份額比較選中陣容與假設對手，平手不算領先；權重衡量小幅改善此類別的局部影響，有訊號時平均為 1。兩者都不是實戰勝率，未涵蓋全部估計誤差。", "muted"));
  } else detail.append(node("p", "這份結果未含類別明細。", "muted"));
  target.append(detail);
}

export function renderPlan(result, players, market, nominate, unavailable) {
  renderFitExplanation(result?.fit);
  el("plan").replaceChildren(); el("nominations").replaceChildren();
  if (!result) { el("planNote").textContent = `${unavailable}；完成後顯示目前狀態的方案。`; return; }
  if (result.plan.reason) { el("planNote").textContent = `無合法方案：${result.plan.reason}`; return; }
  el("planNote").textContent = `尚需 ${result.plan.purchases.length} 人 · 預計支出 ${money(result.plan.cost)}${result.fit ? ` · 陣容調整步長 ${result.fit.selected_step}${result.fit.method === "paired_managed_marginal" ? ` · 管理情境估計（傷兵替補${result.fit.policy.streaming_slots ? "、串流" : ""}${result.fit.policy.upgrades ? "、升級" : ""}）` : ""}` : ""}`;
  const quotes = new Map(market.prices.map(p => [p.player_id,p]));
  el("plan").replaceChildren(...result.plan.players.map(id => {
    const row = node("div", "", "plan-row"), owned = !result.plan.purchases.includes(id);
    row.append(action(players.get(id)?.name ?? id, () => nominate(id)), node("span", owned ? "已持有" : money(quotes.get(id)?.planning_cost))); return row;
  }));
  for (const [mode,label] of [["target","難替代／組隊目標"],["drain","讓對手消耗預算"]]) {
    const group = node("div", "", "nomination-group"), ids = result.nominations[mode];
    group.append(node("h3", `${label}${result.nominations.mode === mode ? " · 優先" : ""}`));
    group.append(node("p", mode === "target" ? "方案內且停損價足以支付規劃成本；優先處理難替代的位置。" : "方案外、預期成交高於我方上限，且至少兩個對手仍能競價；不保證有人搶。", "muted"));
    group.append(...ids.map(id => action(players.get(id)?.name ?? id, () => nominate(id))));
    if (!ids.length) group.append(node("p", "目前沒有符合條件的人選。", "muted"));
    el("nominations").append(group);
  }
}

export function renderComparison(value, players, data) {
  const {comparison:c} = value;
  const quotes = new Map(data.market.market.prices.map(p => [p.player_id,p.planning_cost]));
  const owned = new Set(data.market.market.room.find(t => t.id === data.state.mine).owned);
  const budget = data.market.market.room.find(t => t.id === data.state.mine).budget;
  const grid = node("div", "", "comparison-grid");
  for (const [key, title] of [["buy", `買入 ${money(c.price)}`], ["skip", "不買此人"]]) {
    const branch = c[key], col = node("div", ""); col.append(node("strong", title));
    if (branch.reason) col.append(node("p", `無解：${branch.reason}`, "warning"));
    else {
      col.append(node("p", `支出 ${money(branch.cost)} · 留 ${money(budget - branch.cost)}`));
      const other = new Set(c[key === "buy" ? "skip" : "buy"].players ?? []);
      const ordered = [...branch.players].sort((a,b) => Number(other.has(a))-Number(other.has(b)) || a.localeCompare(b,"en"));
      const list = node("ul", ""); ordered.forEach(id => list.append(node("li", `${other.has(id) ? "共同" : "差異"} · ${players.get(id)?.name ?? id} · ${owned.has(id) ? "已持有" : money(id === c.player_id && key === "buy" ? c.price : quotes.get(id))}`))); col.append(list);
    }
    grid.append(col);
  }
  el("comparison").replaceChildren(node("strong", comparisonLabel(c)), node("p", c.delta == null ? "至少一個分支無合法方案" : `組隊效用差 ${c.delta.toFixed(3)}；不是勝率。`, "muted"), grid);
}
import {priceRows, rowTags, comparisonLabel, normalized, floorBackup, forecastRange} from "/presentation.js";
