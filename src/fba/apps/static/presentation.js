export function normalized(value) {
  return String(value ?? "").normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}
export function matches(player, query) {
  const name = normalized(player.name), initials = name.split(" ").map(word => word[0] ?? "").join("");
  const capitals = normalized(player.name.match(/[A-Z]/g)?.join("") ?? "");
  const text = normalized(`${player.name} ${player.id} ${player.detail?.team_id ?? ""} ${player.team?.abbreviation ?? ""} ${player.team?.name ?? ""}`);
  const needle = normalized(query);
  return !needle || needle.split(" ").every(part => text.includes(part)) || text.replaceAll(" ", "").includes(needle.replaceAll(" ", "")) || initials === needle || capitals === needle;
}
export function valueGap(fair, expected) {
  if (fair == null || expected == null) return {difference:null, discount:null, focused:false};
  const difference = fair - expected, discount = fair > 0 ? difference / fair : null;
  return {difference, discount, focused:difference >= 5 && discount >= 0.2};
}
export function priceRows(players, desk, result, watched, filters) {
  const sales = new Map(desk.state.sales.map(s => [s.player_id, s]));
  const quotes = new Map(desk.market.market.prices.map(q => [q.player_id, q]));
  const caps = new Map(result?.caps.map(c => [c.player_id, c]) ?? []);
  const plan = new Set(result?.plan.purchases ?? []);
  const flex = new Set(result?.streaming?.flex.map(p => p.player_id) ?? []);
  const rows = [...players.values()].map(player => {
    const quote = quotes.get(player.id), cap = caps.get(player.id), sale = sales.get(player.id);
    const gap = valueGap(player.fair, quote?.expected);
    const edge = cap?.amount == null || quote?.expected == null ? null : cap.amount - quote.expected;
    return {player, quote, cap, sale, ...gap, edge, inPlan:plan.has(player.id),flex:flex.has(player.id)};
  }).filter(row => visible(row, watched, filters));
  return rows.sort((a,b) => compareRows(a,b,filters.sort) || a.player.id.localeCompare(b.player.id, "en"));
}
function visible(row, watched, {query,scope,position}) {
  if (!matches(row.player, query) || (position && !row.player.positions.includes(position))) return false;
  if (scope === "all") return true;
  if (row.sale) return false;
  if (scope === "watch") return watched.has(row.player.id);
  if (scope === "focus") return row.focused;
  return scope !== "value" || row.difference > 0;
}
function compareRows(a,b,sort) {
  if (sort === "name") return a.player.name.localeCompare(b.player.name, "en");
  const value = row => ({gap:row.difference, cap:row.cap?.amount, fair:row.player.fair, edge:row.edge})[sort];
  const av = value(a), bv = value(b);
  return av == null ? (bv == null ? 0 : 1) : bv == null ? -1 : bv - av;
}
export function rowTags(row) {
  const {player, quote, cap, inPlan, difference} = row, tags = [];
  if (row.flex) tags.push("可操作候選");
  else if (inPlan) tags.push("組隊方案");
  if (!row.sale && difference > 0) tags.push(row.focused ? "重點價差" : "市場低估");
  if (quote?.anchor == null) tags.push("缺市場報價");
  if (!player.positions_confirmed) tags.push("位置待確認");
  if (cap?.forced) tags.push("難以替代");
  const detail = player.detail;
  if (detail?.original_expected_games != null && detail.original_expected_games < detail.healthy_games_threshold) tags.push("出賽風險");
  if (detail?.history_games != null && detail.history_games < detail.history_minimum) tags.push("歷史樣本少");
  if (player.detail?.adjustments.some(a => a.operation.kind === "return_at")) tags.push("傷情待追蹤");
  return tags;
}
export function floorBackup(quote, cap, sold, minimum) {
  return !sold && quote?.planning_cost === minimum && cap?.amount >= minimum && !cap.reason;
}
export function priceCSV(rows, desk, mode) {
  const header = ["球員", "球員 ID", "位置", "NBA 球隊", "公允價", "預期成交", "價差", "折扣比例", "停損價", "可出價空間", "標籤", "買家", "成交價", "估值模式", "狀態雜湊"];
  const teams = new Map(desk.state.teams.map(t => [t.id,t.name]));
  const values = rows.map(r => [r.player.name,r.player.id,r.player.positions.join("/"),r.player.team?.abbreviation ?? r.player.detail?.team_id,r.player.fair,r.quote?.expected,r.difference,r.discount,r.cap?.amount,r.edge,rowTags(r).join(" / "),teams.get(r.sale?.buyer),r.sale?.amount,mode,desk.market.state_sha256]);
  return "\ufeff" + [header,...values].map(row => row.map(csvCell).join(",")).join("\r\n") + "\r\n";
}
function csvCell(value) {
  let text = value == null ? "" : String(value);
  if (typeof value === "string" && /^[\s\u0000-\u001f]*[=+@-]/.test(text)) text = "'" + text;
  return `"${text.replaceAll('"','""')}"`;
}
export function comparisonLabel(comparison) {
  if (comparison.buy.reason && comparison.skip.reason) return "兩種選擇都無合法方案";
  if (comparison.buy.reason) return "此價格買入後無法完成組隊";
  if (comparison.skip.reason) return "目前組隊需要買入此人";
  return comparison.delta > 0 ? "買入方案的模型效用較高" : comparison.delta < 0 ? "不買方案的模型效用較高" : "兩種方案的模型效用相同";
}
export function forecastRange(player) {
  if (!player.scenarios?.length) return {status:"absent"};
  const values = [player.fair, ...player.scenarios.map(s => s.fair)];
  if (values.some(v => v == null)) return {status:"incomplete"};
  return {status:"ready",low:Math.min(...values),high:Math.max(...values)};
}
