import { el, field, select, form, table, number, percent, formula, empty, minutesChart, calibrationChart } from "/forms.js";

const section = (title, ...children) => el("section", {}, el("h2", {}, title), children);
const jsonDetails = (title, value) => el("details", {}, el("summary", {}, title), el("pre", {}, JSON.stringify(value, null, 2)));
const dateText = (value, zone) => value ? new Intl.DateTimeFormat("zh-TW", { dateStyle: "short", timeStyle: "short", timeZone: zone }).format(new Date(value)) : "—";
const playerName = (ctx, id) => ctx.data.projection?.players.find(p => p.player.id === id)?.player.name ?? id;
const checks = (ctx, players, name) => el("div", { class: "checklist" }, players.map(id => el("label", {}, el("input", { type: "checkbox", name, value: id }), playerName(ctx, id))));

export function syncView(ctx) {
  const data = ctx.data;
  const auth = form([
    el("p", {}, "使用自己的 Yahoo API 憑證。憑證與 refresh token 會存入作業系統憑證庫。"),
    el("ol", {}, el("li", {}, el("a", { href: "https://sports.yahoo.com/developer/access/", target: "_blank", rel: "noreferrer" }, "申請 Yahoo Fantasy API 存取")), el("li", {}, "在 Yahoo Developer Network 建立應用，啟用 Fantasy Sports 唯讀權限。"), el("li", {}, "輸入 Client ID 與 Client Secret，完成授權後貼回授權碼。")),
    field("Client ID", "client_id", "", "password", { autocomplete: "off", required: true }),
    field("Client Secret", "client_secret", "", "password", { autocomplete: "off", required: true })
  ], async (values, node) => {
    const result = await ctx.run("connect", Object.fromEntries(values), false);
    node.reset();
    ctx.open(section("連結 Yahoo", el("a", { href: result.url, target: "_blank", rel: "noreferrer", class: "primary" }, "開啟 Yahoo 授權頁"), form([field("Yahoo 授權碼", "code", "", "password", { required: true, autocomplete: "off" })], async (v, f) => { await ctx.run("authorize", Object.fromEntries(v)); f.reset(); ctx.close(); }, "完成連結", ctx.error)));
  }, "連結 Yahoo", ctx.error);
  const credentialStatus = data.state?.sync.connected && !data.state.sync.authorization_valid ? "需要重新連結" : data.credentials.status;
  const nodes = [section(data.state ? "帳號與聯盟" : "第一次使用",
    table(["Yahoo 帳號 ID", "聯盟", "憑證狀態", "上次成功同步", "下次排程"], [[data.credentials.account_id ?? "尚無帳號識別", data.state?.selected.name ?? "尚未選擇", credentialStatus, dateText(data.state?.sync.last_success, data.preferences.timezone), data.selected ? dateText(data.next_sync_at, data.preferences.timezone) : "選擇聯盟後啟用"]]), auth)];
  nodes.push(section("選擇聯盟", el("button", { onClick: () => ctx.run("discover", {}).catch(ctx.error) }, "讀取本季聯盟"), data.leagues?.length ? form([select("本季 NBA 聯盟", "key", data.leagues.map(l => [l.key, `${l.name} · ${l.season}`]), data.selected)], async v => { await ctx.run("select", Object.fromEntries(v)); await ctx.run("sync", {}); }, "選擇並同步", ctx.error) : empty("完成 Yahoo 授權後列出本季聯盟。")));
  if (!data.state) return el("div", {}, nodes, sourcesSection(ctx));
  nodes.push(settingsSection(ctx));
  nodes.push(sourcesSection(ctx));
  nodes.push(section("樣本外驗證", el("p", {}, data.calibration?.reason ?? "尚無報告"), form([
    field("通過驗證的報告 JSON 完整路徑", "path", "", "text", {required:true})
  ], async v => ctx.run("validation", Object.fromEntries(v)), "驗證並套用報告參數", ctx.error)));

  const unresolved = [...new Set([...(data.state.sync.unresolved_rostered ?? []), ...(data.unresolved_free ?? [])])];
  nodes.push(section("球員身分對照", unresolved.length ? table(["Yahoo player key", "指定內部 ID"], unresolved.map(id => [id, form([field("內部球員 ID", "player_id", "", "text", { required: true })], async v => ctx.run("mapping", { external_id: id, player_id: v.get("player_id") }), "對照", ctx.error)])) : el("p", { class: "muted" }, "目前沒有未對照的隊伍名單球員。")));
  nodes.push(section("同步紀錄", data.sync_log?.length ? table(["時間", "資料集", "筆數", "請求", "耗時", "結果", "快照"], data.sync_log.map(r => [dateText(r.at, data.preferences.timezone), r.dataset ?? "同步", r.rows ?? "—", r.requests, `${number(r.elapsed_seconds, 2)} 秒`, r.error ?? r.result, el("span", { class: "hash" }, r.sha256 ?? "—")])) : empty("還沒有同步紀錄。")));
  nodes.push(section("偏好", form([field("完整偏好設定", "preferences", JSON.stringify(data.preferences, null, 2), "textarea")], async v => ctx.run("preferences", JSON.parse(v.get("preferences"))), "儲存偏好", ctx.error)));
  return el("div", {}, nodes);
}

function settingsSection(ctx) {
  const draft = ctx.data.state.draft;
  if (!draft) return section("聯盟設定", empty("同步 Yahoo 後會產生設定草稿，確認後才啟用計算。"));
  const changes = ctx.data.differences ?? [];
  return section("確認聯盟設定", el("p", { class: "warning" }, `請確認這些 Yahoo 未完整提供或需要核對的欄位：${draft.confirm_fields.join("、")}`), changes.length ? table(["項目", "本機值", "Yahoo 值"], changes.map(c => [c.key, JSON.stringify(c.local), JSON.stringify(c.yahoo)])) : el("p", { class: "muted" }, "Yahoo 設定與已確認的本機設定一致。"), form([field("設定草稿", "document", JSON.stringify(draft.document, null, 2), "textarea")], async v => ctx.run("settings", { document: JSON.parse(v.get("document")), decision: "import" }), "確認並匯入", ctx.error), ctx.data.state.league ? el("button", { onClick: () => ctx.run("settings", { document: {}, decision: "keep" }).catch(ctx.error) }, "保留本機設定") : null);
}

function sourcesSection(ctx) {
  const source = ctx.data.state?.sources ?? ctx.data.workspace?.sources ?? {};
  return section("球員資料與賽季前預測", el("p", {}, "選用已獲授權的 NBA 資料服務。賽季前 forecast.json 只讀取，並以 SHA-256 鎖定版本。"), form([
    field("forecast.json 完整路徑", "forecast_path", source.forecast_path, "text", { required: true }), field("預測檔 SHA-256", "forecast_sha256", source.forecast_sha256, "text", { required: true, pattern: "[a-f0-9]{64}" }), field("預測公開時間（含時區）", "forecast_known_at", source.forecast_known_at, "text", { required: true, placeholder: "ISO 8601" }), field("已授權 NBA JSON 來源 HTTPS URL", "player_url", source.player_url, "url", { required: true }), field("來源授權依據", "permission_reference", source.permission_reference, "text", { required: true })
  ], async v => ctx.run("sources", Object.fromEntries(v)), "載入並鎖定來源", ctx.error));
}

export function teamsView(ctx) {
  const projection = ctx.data.projection;
  if (!projection) return section("球隊與有效預測", empty(ctx.data.projection_error ?? "先在資料與同步頁載入賽季前預測與 NBA 資料來源。"));
  const summaries = ctx.data.team_views;
  const order = ctx.teamOrder ?? "flags";
  const teams = [...summaries].sort((a, b) => (order === "games" ? (b.week_games ?? -1) - (a.week_games ?? -1) : flagCount(projection, b.team_id) - flagCount(projection, a.team_id)) || a.team_id.localeCompare(b.team_id));
  const summary = teams.find(t => t.team_id === ctx.nbaTeam) ?? teams[0];
  const selected = summary.team_id;
  const players = projection.players.filter(p => p.player.team_id === selected).sort((a, b) => Math.round(b.minutes / ctx.data.parameters.tolerance.value) - Math.round(a.minutes / ctx.data.parameters.tolerance.value) || a.player.id.localeCompare(b.player.id));
  const sort = select("球隊排序", "teamOrder", [["flags", "需要確認優先"], ["games", "本週比賽數"]], order);
  sort.querySelector("select").addEventListener("change", e => {ctx.teamOrder = e.target.value; ctx.render();});
  const nav = el("aside", {}, section("NBA 球隊", sort, el("div", { class: "team-list" }, teams.map(t => el("button", { class: selected === t.team_id ? "selected" : "", onClick: () => ctx.selectNba(t.team_id) }, t.team_id, el("span", { class: "badge" }, `${flagCount(projection, t.team_id)} 提醒 · ${t.week_games ?? "—"} 場`))))));
  const roster = ctx.data.snapshot;
  const owner = pid => roster?.teams.find(t => [...t.players, ...Object.keys(t.injury_players)].includes(pid))?.name ?? (roster ? "自由球員" : "—");
  const counts = [...new Set([ctx.data.parameters.override_window.value, ctx.data.parameters.role_window.value])];
  const multiplierFields = ctx.data.parameters.fields.filter(f => f.kind === "multiply");
  const tableRows = players.map(p => {
    const detail = summary.players.find(d => d.player_id === p.player.id);
    const adjustments = p.adjustments.map(e => `${e.reason} · 到 ${e.ends_on}`).join("\n");
    return [el("button", { class: "name", onClick: () => ctx.player(p) }, p.player.name), el("span", {class: detail.manual_status ? "adjusted" : "", title:adjustments}, detail.manual_status ? `${p.player.status} → 手調 ${detail.manual_status}` : p.player.status), detail.manual_return_on ? `手調 ${detail.manual_return_on} · 來源 ${detail.return_on ?? "未提供"}` : detail.return_on ?? "—", number(p.traces.minutes.result, 1), el("button", { class: p.adjustments.some(e => ctx.data.parameters.fields.find(f => f.id === e.field)?.targets.includes("minutes")) ? "small adjusted" : "small", onClick: () => ctx.edit(p), title: adjustments }, number(p.minutes, 1)),
      ...counts.map(n => detail.recent_minutes[n] ? el("div", {}, number(detail.recent_minutes[n].result, 1), formula(detail.recent_minutes[n], ctx.data.formulas)) : "—"),
      ...multiplierFields.map(f => detail.multipliers[f.id] ? el("span", {class:"adjusted", title:adjustments}, number(detail.multipliers[f.id].result), formula(detail.multipliers[f.id], ctx.data.formulas)) : "1"),
      owner(p.player.id), el("span", { class: "warning" }, p.flags.length ? p.flags.map(f => f.reason).join("；") : "—")];
  });
  const content = el("div", {}, section(selected,
    el("div", { class: "row" }, el("strong", {}, `全隊預期分鐘 ${number(summary.minutes.result, 1)} / ${number(summary.budget.result, 1)}`), el("span", { class: summary.difference.result ? "warning" : "muted" }, `差距 ${number(summary.difference.result, 1)}；只提醒，不自動調整`)),
    [summary.minutes, summary.budget, summary.difference].map(t => formula(t, ctx.data.formulas)),
    el("p", {}, `本週 ${summary.week_games ?? "—"} 場 · 下週 ${summary.next_week_games ?? "—"} 場`),
    el("p", { class: "muted" }, `背靠背第二天：${summary.back_to_back.join("、") || "沒有已公布場次"}`),
    table(["球員", "狀態", "回歸日（來源／手調）", "模型分鐘", "有效分鐘／調整", ...counts.map(n => `近 ${n} 場`), ...multiplierFields.map(f => f.label), "聯盟歸屬", "需要確認"], tableRows)), ledgerSection(ctx));
  return el("div", { class: "split" }, nav, content);
}
function flagCount(projection, team) { return projection.players.filter(p => p.player.team_id === team).reduce((v, p) => v + p.flags.length, 0); }

function ledgerSection(ctx) {
  const entries = (ctx.data.ledger ?? []).filter(e => !ctx.ledgerState || e.state === ctx.ledgerState);
  const filter = select("帳本狀態", "ledgerState", [["", "全部"], ...["生效中", "未生效", "已到期", "已撤銷", "撤銷紀錄"].map(s => [s, s])], ctx.ledgerState ?? "");
  filter.querySelector("select").addEventListener("change", e => {ctx.ledgerState = e.target.value; ctx.render();});
  return section("手調帳本", filter, entries.length ? table(["球員", "欄位", "值", "生效期間", "理由", "狀態", "操作"], entries.map(e => [playerName(ctx, e.player_id), e.field, e.value, `${e.starts_on} — ${e.ends_on}`, e.reason, e.state, !e.revokes.length && e.state !== "已撤銷" ? el("button", { class: "small", onClick: () => ctx.run("revoke", { expected_sha256: ctx.data.ledger_sha256, group_id: e.group_id }).catch(ctx.error) }, "撤銷整組") : "—"])) : empty("尚無手調。每筆修改和撤銷都會保留紀錄。"));
}

export function playerCard(ctx, player) {
  const traces = keys => el("div", {}, keys.filter(k => player.traces[k]).map(k => formula(player.traces[k], ctx.data.formulas)));
  const manual = target => player.adjustments.filter(a => ctx.data.parameters.fields.find(f => f.id === a.field)?.targets.includes(target)).map(a => `${a.value} · ${a.starts_on} 至 ${a.ends_on} · ${a.reason}`).join("；") || "—";
  const rows = Object.entries(player.rates).map(([stat, rate]) => [stat,
    number(player.prior.rates[stat]), number(player.traces[`current:${stat}`]?.result),
    number(player.traces[stat].result), percent(player.weights[stat]), ctx.data.parameters.rate_k[stat].value,
    manual(`rate:${stat}`), number(rate), traces([stat, `current:${stat}`, `weight:${stat}`, `final:${stat}`, `expected:${stat}`])]);
  const shots = Object.entries(player.probabilities).map(([stat, value]) => [stat,
    percent(player.prior.probabilities[stat]), percent(player.traces[`current:${stat}`]?.result),
    percent(player.traces[stat].result), percent(player.weights[stat]), ctx.data.parameters.shot_k[stat].value,
    manual(`shot:${stat}`), percent(value), traces([stat, `current:${stat}`, `weight:${stat}`])]);
  return section(player.player.name,
    el("p", {}, `${player.player.team_id} · ${player.player.positions.join(" / ")} · 出賽 ${percent(player.probability)} · 有效分鐘 ${number(player.minutes, 1)}`),
    el("p", {}, `分鐘手調：${manual("minutes")} · 出賽手調：${manual("q")}`),
    minutesChart(player, ctx.data.parameters.fields), formula(player.traces.minutes, ctx.data.formulas),
    table(["每分鐘數據／命中率", "先驗", "當季", "混合", "當季權重", "k", "手調", "最終值", "算式"], [...rows, ...shots]),
    player.flags.map(f => el("div", { class: "flag" }, f.reason, el("p", {}, `模型 ${number(f.model)} · 觀察 ${number(f.observed)}`), (f.traces ?? []).map(t => formula(t, ctx.data.formulas)), el("button", { class: "small", onClick: () => ctx.edit(player, f) }, "建立手調"), el("button", { class: "small", onClick: () => ctx.ignore(f) }, "暫時忽略"))),
    el("p", {}, `來源 ${ctx.data.player_source?.source ?? "—"} · 資料時間 ${dateText(ctx.data.player_source?.as_of, ctx.data.preferences.timezone)}`),
    el("p", { class: "hash" }, `球員快照 ${ctx.data.state?.players_sha256 ?? ctx.data.workspace?.players_sha256} · 預測快照 ${ctx.data.state?.priors_sha256 ?? ctx.data.workspace?.priors_sha256}`));
}

export function weekView(ctx) {
  const day = ctx.data.projection?.on ?? new Date().toLocaleDateString("en-CA");
  const weeks = ctx.data.state.league.matchups.filter(w => w.end >= day);
  const result = ctx.results.week;
  const selector = form([select("對戰週", "week_id", weeks.map(w => [w.id, `${w.id} · ${w.start} — ${w.end}`]), result?.week_id)], async v => { ctx.results.week = await ctx.run("week", Object.fromEntries(v)); ctx.render(); }, "計算對戰", ctx.error);
  const nodes = [section("每週對戰", selector)];
  if (!result) return el("div", {}, nodes, empty("選擇對戰週，計算已打完的實際值與剩餘合法先發。"));
  nodes.push(forecastCard(ctx, result));
  nodes.push(section("自由球員計畫", el("p", { class: "muted" }, "假設對手名單不變；所有加人與丟人都由你在 Yahoo 操作。"), !ctx.data.calibration.enabled ? el("p", { class: "warning" }, ctx.data.calibration.reason) : el("button", { class: "primary", onClick: async () => { try { ctx.results.plans = await ctx.run("recommendations", { week_id: result.week_id }); ctx.render(); } catch (e) { ctx.error(e); } } }, "搜尋換人建議"), (ctx.results.plans ?? []).slice(0, 10).map(plan => el("section", { class: "plan" }, el("h3", {}, plan.moves.map(m => `${m.effective_on}：${playerName(ctx, m.add)} ↔ ${playerName(ctx, m.drop)}`).join("；")), el("p", {}, `本週 Δ ${number(plan.delta_week)} · 剩餘賽季 Δ ${number(plan.delta_season)} · 分數 ${number(plan.score)}`), plan.traces.map(t => formula(t, ctx.data.formulas)), table(["加人", "可先發場次"], plan.moves.map(m => [playerName(ctx, m.add), m.starter_games])), tradeCategories(ctx, "本週各類別勝率前後", [plan.before], [plan.after], plan.category_changes), el("button", { onClick: () => { ctx.results.selectedPlan = plan.id; ctx.navigate("today"); } }, "放入今日待辦"), el("label", {class:"checklist"}, el("input", {type:"checkbox", checked:ctx.data.notes?.adopted?.[plan.id] === true, onChange:e => ctx.run("adopt", {id:plan.id, completed:e.target.checked}).catch(ctx.error)}), "我已在 Yahoo 採納這個計畫")))));
  return el("div", {}, nodes);
}

function forecastCard(ctx, result) {
  return section("這週怎麼贏", el("div", { class: "scoreline" }, el("div", {}, el("span", { class: "large-number" }, result.scoring === "h2h_one_win" ? percent(result.score) : number(result.score, 2)), el("span", { class: "muted" }, result.scoring === "h2h_one_win" ? " 整週勝率" : " 預期贏的類別數")), el("p", { class: "muted" }, `模擬 ${result.simulations} 次 · 誤差 ±${number(result.standard_error, 4)} · 校準 c=${ctx.data.parameters.calibration.value}`)), table(["類別", "我方", "對手", "原始勝率", "校準後", "判斷", "核對"], result.categories.map(c => [c.label, c.home_denominator === null ? number(c.home) : `${number(c.home_numerator)} / ${number(c.home_denominator)} = ${number(c.home)}`, c.away_denominator === null ? number(c.away) : `${number(c.away_numerator)} / ${number(c.away_denominator)} = ${number(c.away)}`, percent(c.raw_probability), percent(c.probability), { safe: "穩贏", key: "關鍵", abandon: "放掉" }[c.strategy], el("div", {}, c.traces.map(t => formula(t, ctx.data.formulas)))])), result.traces.map(t => formula(t, ctx.data.formulas)), jsonDetails("逐日合法先發", result.lineups));
}

export function tradesView(ctx) {
  const snapshot = ctx.data.snapshot;
  const mine = snapshot.teams.find(t => t.id === snapshot.mine);
  const others = snapshot.teams.filter(t => t.id !== snapshot.mine);
  const selected = others.find(t => t.id === ctx.tradeTeam) ?? others[0];
  const picker = select("交易對象", "opponent", others.map(t => [t.id, t.name]), selected.id);
  picker.querySelector("select").addEventListener("change", e => { ctx.tradeTeam = e.target.value; ctx.render(); });
  const manual = form([picker, el("div", { class: "two-col" }, section("我方送出", checks(ctx, mine.players, "send")), section("我方收到", checks(ctx, selected.players, "receive")))], async v => { ctx.results.trade = await ctx.run("trade", { opponent: v.get("opponent"), send: v.getAll("send"), receive: v.getAll("receive") }); ctx.render(); }, "評估交易", ctx.error);
  const search = el("div", { class: "actions" }, el("button", { onClick: () => searchTrade(ctx, null, 1) }, "全聯盟 1 換 1"), el("button", { onClick: () => searchTrade(ctx, selected.id, 2) }, "此隊最多 2 換 2"), el("button", { onClick: async () => { try { const rows = await ctx.run("partners", {}); ctx.open(section("互補交易對象", table(["對象", "互補分數", "類別", "算式"], rows.map(r => [snapshot.teams.find(t => t.id === r.team_id)?.name ?? r.team_id, number(r.score), r.categories.join("、"), el("div", {}, r.traces.map(t => formula(t, ctx.data.formulas)))])))); } catch (e) { ctx.error(e); } } }, "找互補對象"));
  const trade = ctx.results.trade;
  const sort = ctx.tradeSort ?? "expected_gain";
  const sorts = el("div", {class:"actions"}, [["expected_gain", "期望值"], ["mine_delta", "增益最大"], ["acceptance", "最可能成交"]].map(([key, label]) => el("button", {class: sort === key ? "selected" : "", onClick: () => {ctx.tradeSort = key; ctx.render();}}, label)));
  const rankedTrades = [...(ctx.results.trades ?? [])].sort((a,b) => Math.round(b[sort]/ctx.data.parameters.tolerance.value) - Math.round(a[sort]/ctx.data.parameters.tolerance.value) || a.opponent.localeCompare(b.opponent) || a.send.join().localeCompare(b.send.join()) || a.receive.join().localeCompare(b.receive.join()));
  return el("div", {}, section("交易分析", manual), trade ? tradeCard(ctx, trade) : null, section("自動搜尋", search, sorts, ctx.results.trades?.length ? table(["對象", "送出", "收到", "我方增益", "對方 ΔN", "接受率", "期望值"], rankedTrades.slice(0, 30).map(t => [t.opponent, t.send.map(p => playerName(ctx, p)).join("、"), t.receive.map(p => playerName(ctx, p)).join("、"), number(t.mine_delta), number(t.opponent_delta), percent(t.acceptance), el("button", { class: "small", onClick: () => ctx.open(tradeCard(ctx, t)) }, number(t.expected_gain))])) : null), proposalsSection(ctx));
}
async function searchTrade(ctx, opponent, size) {
  try { ctx.results.trades = await ctx.run("trade-search", { opponent, size }); ctx.render(); } catch (e) { ctx.error(e); }
}
function tradeCategories(ctx, label, before, after, changes) {
  return el("details", {}, el("summary", {}, label), table(["週", "類別", "交易前", "交易後", "差異"], before.flatMap(w => {
    const changed = after.find(a => a.week_id === w.week_id);
    return w.categories.map(c => {const next = changed.categories.find(n => n.id === c.id); return [w.week_id, c.label, percent(c.probability), percent(next.probability), changes?.[w.week_id]?.[c.id] ? formula(changes[w.week_id][c.id], ctx.data.formulas) : "—"];});
  })));
}
function tradeCard(ctx, trade) {
  return section("交易評估", el("p", { class: "large-number" }, `${number(trade.mine_delta)} 勝`), el("p", {}, `季後賽機率 Δ ${percent(trade.playoff_delta)} · 季後賽場次 Δ ${number(trade.playoff_games_delta)}`), el("p", {}, `對方公開價值 ΔR ${number(trade.rank_delta)} · 需求 ΔN ${number(trade.opponent_delta)} · 接受率 ${percent(trade.acceptance)} `, el("span", { class: "badge" }, trade.calibrated ? "已校正" : "未校正")), trade.traces.map(t => formula(t, ctx.data.formulas)), tradeCategories(ctx, "我方各週類別勝率前後", trade.before, trade.after, trade.category_changes), tradeCategories(ctx, "對方各週類別勝率前後", trade.opponent_before, trade.opponent_after, trade.opponent_category_changes), jsonDetails("自動補人／丟人與交易後名單", { adds: trade.automatic_adds, drops: trade.automatic_drops, rosters: trade.rosters }), el("button", { onClick: () => ctx.run("proposal", { opponent: trade.opponent, send: trade.send, receive: trade.receive, outcome: "pending", supersedes: null }).catch(ctx.error) }, "記錄我已在 Yahoo 提案"));
}
function proposalsSection(ctx) {
  return section("提案紀錄", el("p", { class: "muted" }, "只記錄你手動提出的交易，不會替你送出。"), table(["日期", "對象", "預測機率", "結果", "更新"], (ctx.data.proposals ?? []).map(p => [dateText(p.created_at, ctx.data.preferences.timezone), p.opponent, percent(p.probability), p.outcome, el("div", { class: "actions" }, ["accepted", "rejected", "withdrawn"].map(outcome => el("button", { class: "small", onClick: () => ctx.run("proposal", { opponent: p.opponent, send: p.send, receive: p.receive, outcome, supersedes: p.id }).catch(ctx.error) }, { accepted: "接受", rejected: "拒絕", withdrawn: "撤回" }[outcome])))])), el("button", { onClick: async () => { try { const fit = await ctx.run("refit-acceptance", {}); ctx.open(section("接受模型擬合報告", el("p", {}, `${fit.count} 筆提案 · Log loss ${number(fit.log_loss, 6)} · 訓練樣本內評估`), calibrationChart(fit.bins), jsonDetails("係數與各區間", fit))); } catch (e) { ctx.error(e); } } }, "用已結案提案重新擬合"));
}

export function todayView(ctx) {
  const result = ctx.results.today;
  const selector = form([
    field("日期", "on", result?.on ?? new Date().toLocaleDateString("en-CA"), "date")
  ], async v => {
    ctx.results.today = await ctx.run("today", {
      on: v.get("on"), plan_id: ctx.results.selectedPlan ?? null
    });
    ctx.render();
  }, "計算今日安排", ctx.error);
  const heading = section("今日排陣", selector);
  if (!result) return el("div", {}, heading, empty("依本週對戰目標求解合法先發。"));
  const actions = result.actions.map(a => el("li", {}, el("label", {},
    el("input", {
      type: "checkbox", checked: a.completed,
      onChange: e => ctx.run("complete", {id: a.id, completed: e.target.checked}).catch(ctx.error)
    }),
    el("span", {}, `${playerName(ctx, a.player_id)} · ${a.slot ?? a.kind}`,
      el("small", {}, `　${a.reason}`))
  )));
  const locks = Object.entries(result.locks).map(([p, lock]) => [
    playerName(ctx, p), dateText(lock, ctx.data.preferences.timezone),
    new Date(lock) <= new Date() ? "已鎖定" : `${number((new Date(lock) - new Date()) / 60000, 0)} 分鐘`
  ]);
  return el("div", {}, heading, section("今天要做的事",
    el("p", {}, `本週分數 ${number(result.score_before)} → ${number(result.score_after)}`),
    result.traces.map(t => formula(t, ctx.data.formulas)),
    el("ul", {class: "checklist"}, actions),
    table(["球員", "今日對手", "開賽時間", "狀態", "建議位置", "理由", "本週影響"], result.players.map(p => [
      playerName(ctx,p.player_id), p.opponents.join("、") || "—",
      p.tipoffs.map(t=>dateText(t,ctx.data.preferences.timezone)).join("、") || "—",
      p.status, p.slot ?? "板凳", p.reason,
      p.marginal ? formula(p.marginal, ctx.data.formulas) : "—"
    ])),
    table(["球員", "鎖定時間", "距離鎖定"], locks),
    result.drop_assessment ? section("丟人比較",
      table(["最弱名單", "剩餘價值", "最佳自由球員", "替換增益", "持有率", "持有率走勢"], [[
        playerName(ctx, result.drop_assessment.drop), number(result.drop_assessment.remaining_value_lost),
        result.drop_assessment.add ? playerName(ctx, result.drop_assessment.add) : "無合法候選",
        number(result.drop_assessment.replacement_gain), percent(result.drop_assessment.ownership),
        percent(result.drop_assessment.ownership_change)
      ]]), result.drop_assessment.traces.map(t => formula(t, ctx.data.formulas))) : null
  ));
}

export function reviewView(ctx) {
  const download = el("button", {onClick: async () => {try {
    const records = await ctx.run("calibration-history", {}, false);
    const url = URL.createObjectURL(new Blob([JSON.stringify(records, null, 2)], {type:"application/json"}));
    const link = el("a", {href:url, download:`calibration-${records.season_id}.json`});
    link.click(); setTimeout(()=>URL.revokeObjectURL(url), 1000);
  } catch(e) {ctx.error(e);}}}, "匯出下一季校準輸入");
  const reports = ctx.data.reports ?? [];
  const cumulative = ctx.data.cumulative_review;
  const summary = cumulative?.weeks ? section("累積模型表現",
    table(["週數", "類別預測筆數", "類別 Brier", "整週 Brier"], [[cumulative.weeks,
      cumulative.category_predictions, number(cumulative.category_brier), number(cumulative.week_brier)]]),
    (cumulative.traces ?? []).map(t => formula(t, ctx.data.formulas)), calibrationChart(cumulative.bins), table(["機率區間", "筆數", "預測", "實際", "偏差"], cumulative.bins.map(b => [
      `${percent(b.lower)} — ${percent(b.upper)}`, b.count, percent(b.predicted), percent(b.observed), percent(b.difference)
    ]))) : null;
  return el("div", {}, summary, download, section("每週回顧與模型監控", el("p", {}, "使用當時已保存的預測紀錄對照 Yahoo 最終結果；事後變更資料不會改寫過去預測。")), reports.length ? reports.map(r => section(`對戰週 ${r.week_id}`, el("p", { class: r.refit_alert ? "warning" : "muted" }, r.refit_alert ? "校準偏離超過門檻，請重新擬合校準係數。" : "校準偏離未超過設定門檻。"), table(["類別 Brier", "整週 Brier", "有手調誤差", "無手調誤差"], [[number(r.category_brier), number(r.week_brier), number(r.with_adjustments_mae), number(r.without_adjustments_mae)]]), calibrationChart(r.bins), table(["機率區間", "筆數", "平均預測", "實際", "偏差"], r.bins.map(b => [`${percent(b.lower)} — ${percent(b.upper)}`, b.count, percent(b.predicted), percent(b.observed), percent(b.difference)])), r.traces.map(t => formula(t, ctx.data.formulas)), jsonDetails("逐項預測", r.rows), table(["計畫", "採納狀態", "當時預測增益", "該週實際得分"], r.recommendation_outcomes.map(o => [o.plan_id, o.adopted === true ? "已採納" : o.adopted === false ? "未採納" : "未標記", number(o.predicted_gain), number(o.actual_week_score)])), el("p", {class:"muted"}, "實際得分反映整週結果；未採納計畫沒有已觀察到的反事實增益。"))) : empty("尚無已結束且有事前預測紀錄的對戰週。每週最終同步後會自動產生報告。"));
}
