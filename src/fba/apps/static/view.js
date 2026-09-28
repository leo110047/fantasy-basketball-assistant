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

let roomHash = null;
export function renderRoom(data, players) {
  if (roomHash === data.market.state_sha256) return;
  roomHash = data.market.state_sha256;
  const {state, market} = data;
  const names = new Map(state.teams.map(t => [t.id, t.name]));
  const own = market.market.room.find(t => t.id === state.mine);
  el("teamTitle").textContent = names.get(state.mine);
  el("myBudget").replaceChildren(node("strong", money(own.budget)), node("div", `剩 ${own.slots} 格 · 最高可付 ${money(own.maximum_bid)}`));
  el("room").replaceChildren(...market.market.room.map(t => {
    const d = node("details", "", t.id === state.mine ? "mine" : "");
    d.append(node("summary", `${names.get(t.id)}${t.id === state.mine ? "（我方）" : ""} · ${money(t.budget)} / ${t.slots} 格`));
    t.owned.forEach(id => d.append(node("p", `${players.get(id)?.name ?? id} · ${money(state.sales.find(s => s.player_id === id)?.amount)}`)));
    return d;
  }));
  el("history").replaceChildren(...state.sales.map(s => node("li", `${players.get(s.player_id)?.name ?? s.player_id} → ${names.get(s.buyer)} · ${money(s.amount)}`)));
  const last = state.sales.at(-1);
  el("lastSale").textContent = last ? `上一筆：${players.get(last.player_id)?.name ?? last.player_id} · ${names.get(last.buyer)} ${money(last.amount)}` : "尚未成交";
  const buyer = el("buyer").value;
  el("buyer").replaceChildren(option("", "選擇隊伍"), ...state.teams.map(t => option(t.id, `${t.name}${t.id === state.mine ? "（我方）" : ""}`)));
  el("buyer").value = buyer;
}

export function renderTable(data, players, result, watched, browse, nominate, watch) {
  const sold = new Set(data.state.sales.map(s => s.player_id));
  const prices = new Map(data.market.market.prices.map(p => [p.player_id, p]));
  const caps = new Map(result?.caps.map(c => [c.player_id, c]) ?? []);
  const query = el("filter").value.trim().toLowerCase(), scope = el("scope").value;
  const shown = [...players.values()].filter(p => {
    if (!`${p.name} ${p.id}`.toLowerCase().includes(query)) return false;
    if (scope === "all") return true;
    if (sold.has(p.id)) return false;
    if (scope === "watch") return watched.has(p.id);
    return scope !== "value" || (p.fair != null && prices.get(p.id)?.expected != null && p.fair > prices.get(p.id).expected);
  }).sort((a,b) => (b.fair ?? -1) - (a.fair ?? -1) || a.id.localeCompare(b.id, "en"));
  el("count").textContent = `${shown.length} 位`;
  const body = el("players"), existing = new Map([...body.children].map(row => [row.dataset.player, row]));
  const rows = shown.map(p => {
    let row = existing.get(p.id);
    if (!row) {
      row = document.createElement("tr"); row.dataset.player = p.id;
      for (let i=0;i<6;i++) row.append(document.createElement("td"));
      row.children[0].append(action("☆", () => watch(p.id)));
      row.children[1].append(action(p.name, () => browse(p.id), "name"), node("small", ""));
      row.children[4].className = "good";
      row.children[5].append(action("指定", () => nominate(p.id)));
    }
    const cells = row.children, star = cells[0].firstChild, choose = cells[5].firstChild;
    const quote = prices.get(p.id), cap = caps.get(p.id), held = sold.has(p.id);
    text(star, watched.has(p.id) ? "★" : "☆");
    star.setAttribute("aria-label", `追蹤 ${p.name}`); star.setAttribute("aria-pressed", String(watched.has(p.id)));
    text(cells[1].lastChild, p.positions.join(" / "));
    text(cells[2], money(p.fair)); text(cells[3], held ? "已成交" : money(quote?.expected));
    text(cells[4], held ? "—" : result ? `${money(cap?.amount)}${cap?.conditional ? " *" : ""}` : "更新中");
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


export function renderPlan(result, players, market, nominate) {
  el("plan").replaceChildren(); el("nominations").replaceChildren();
  if (!result) { el("planNote").textContent = "更新中；完成後顯示目前狀態的方案。"; return; }
  if (result.plan.reason) { el("planNote").textContent = `無合法方案：${result.plan.reason}`; return; }
  el("planNote").textContent = `尚需 ${result.plan.purchases.length} 人 · 預計支出 ${money(result.plan.cost)}${result.fit ? ` · 陣容調整步長 ${result.fit.selected_step}` : ""}`;
  const quotes = new Map(market.prices.map(p => [p.player_id,p]));
  el("plan").replaceChildren(...result.plan.players.map(id => {
    const row = node("div", "", "plan-row"), owned = !result.plan.purchases.includes(id);
    row.append(action(players.get(id)?.name ?? id, () => nominate(id)), node("span", owned ? "已持有" : money(quotes.get(id)?.planning_cost))); return row;
  }));
  const ids = result.nominations[result.nominations.mode];
  el("nominations").append(node("span", result.nominations.mode === "target" ? "組隊目標" : "讓對手消耗預算", "muted"), ...ids.map(id => action(players.get(id)?.name ?? id, () => nominate(id))));
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
      const list = node("ul", ""); branch.players.forEach(id => list.append(node("li", `${players.get(id)?.name ?? id} · ${owned.has(id) ? "已持有" : money(id === c.player_id && key === "buy" ? c.price : quotes.get(id))}`))); col.append(list);
    }
    grid.append(col);
  }
  el("comparison").replaceChildren(node("p", c.delta == null ? "至少一個分支無合法方案" : `組隊效用差 ${c.delta.toFixed(3)}`), grid);
}
