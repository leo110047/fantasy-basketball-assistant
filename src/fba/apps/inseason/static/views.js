import { el, field, select, form, table, number, percent, formula, empty, minutesChart, calibrationChart, disclosure, playerLabel, scoreText, slotText, avatar } from "/forms.js";

const section = (title, ...children) => el("section", {}, el("h2", {}, title), children);
const jsonDetails = (title, value) => el("details", {}, el("summary", {}, title), el("pre", {}, JSON.stringify(value, null, 2)));
const dateText = (value, zone) => value ? new Intl.DateTimeFormat("zh-TW", { dateStyle: "short", timeStyle: "short", timeZone: zone }).format(new Date(value)) : "—";
const playerName = (ctx, id) => ctx.data.projection?.players.find(p => p.player.id === id)?.player.name ?? id;
function checks(ctx, players, name, selected = []) {
  const rows = players.map(id => el("label", {}, el("input", {type:"checkbox", name, value:id, checked:selected.includes(id)}), playerLabel(ctx,id)));
  const list = el("div", {class:"checklist roster-choices"}, rows);
  if (players.length < 8) return list;
  const search = field("搜尋這份名單", `${name}-filter`, "", "search", {placeholder:"球員姓名", autocomplete:"off"});
  search.querySelector("input").addEventListener("input", event => {
    const query = event.target.value.trim().toLocaleLowerCase();
    rows.forEach(row => {row.hidden = !row.textContent.toLocaleLowerCase().includes(query);});
  });
  return el("div", {}, search, list);
}

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
  const credentialStatus = data.state?.sync.connected && !data.state.sync.authorization_valid ? "需要重新連結" : data.state?.sync.authorization_valid ? "已連結" : data.credentials.status;
  const nodes = [section(data.state ? "帳號與聯盟" : "第一次使用",
    table(["Yahoo 帳號 ID", "聯盟", "憑證狀態", "上次成功同步", "下次排程"], [[data.credentials.account_id ?? "尚無帳號識別", data.state?.selected.name ?? "尚未選擇", credentialStatus, dateText(data.state?.sync.last_success, data.preferences.timezone), data.selected ? dateText(data.next_sync_at, data.preferences.timezone) : "選擇聯盟後啟用"]]), data.state?.sync.authorization_valid ? disclosure("重新連結 Yahoo 帳號", auth) : auth)];
  const leaguePicker = section("選擇聯盟", el("button", { onClick: () => ctx.run("discover", {}).catch(ctx.error) }, "讀取本季聯盟"), data.leagues?.length ? form([select("本季 NBA 聯盟", "key", data.leagues.map(l => [l.key, `${l.name} · ${l.season}`]), data.selected)], async v => { await ctx.run("select", Object.fromEntries(v)); await ctx.run("sync", {}); }, "選擇並同步", ctx.error) : empty("完成 Yahoo 授權後列出本季聯盟。"));
  nodes.push(data.selected ? disclosure("切換聯盟",leaguePicker) : leaguePicker);
  if (!data.state) return el("div", {}, nodes, sourcesSection(ctx));
  nodes.push(preferencesSection(ctx));
  nodes.push(data.state.sync.settings_pending || !data.state.league ? settingsSection(ctx) : disclosure("已確認的聯盟規則", settingsSection(ctx)));
  nodes.push(data.projection ? disclosure("球員資料來源與預測版本", sourcesSection(ctx)) : sourcesSection(ctx));
  nodes.push(disclosure("模型驗證與校準報告", el("p", {}, data.calibration?.reason ?? "尚無報告"), form([
    field("通過驗證的報告 JSON 完整路徑", "path", "", "text", {required:true})
  ], async v => ctx.run("validation", Object.fromEntries(v)), "驗證並套用報告參數", ctx.error)));

  const unresolved = [...new Set([...(data.state.sync.unresolved_rostered ?? []), ...(data.unresolved_free ?? [])])];
  const identitySection = section("球員身分對照", unresolved.length ? table(["Yahoo 球員", "球隊／識別碼", "指定球員"], unresolved.map(id => {
    const info = data.identity_metadata?.[id];
    return [info?.name ?? id, `${info?.team ?? "未知球隊"} · ${id}`, form([
      select("內部球員", "player_id", (data.projection?.players ?? []).map(p => [p.player.id, `${p.player.name} · ${p.player.team_id} · ${p.player.positions.join("/")}`]))
    ], async v => ctx.run("mapping", { external_id:id, player_id:v.get("player_id") }), "確認對照", ctx.error)];
  })) : el("p", {class:"muted"}, "目前沒有未對照球員。唯一識別碼會沿用跨季對照；歧義由你確認。"));
  nodes.push(unresolved.length ? identitySection : disclosure("球員身分對照",identitySection));
  if (data.state.sync.forecast_error) nodes.push(section("本次預測未保存", el("p", {class:"warning"}, data.state.sync.forecast_error)));
  nodes.push(disclosure("同步紀錄與診斷", data.sync_log?.length ? table(["時間", "資料集", "筆數", "請求", "耗時", "結果", "快照"], data.sync_log.map(r => [dateText(r.at, data.preferences.timezone), r.dataset ?? "同步", r.rows ?? "—", r.requests, `${number(r.elapsed_seconds, 2)} 秒`, r.error ?? r.result, el("span", { class: "hash" }, r.sha256 ?? "—")])) : empty("還沒有同步紀錄。")));
  nodes.push(disclosure("進階偏好 JSON", form([field("完整偏好設定", "preferences", JSON.stringify(data.preferences, null, 2), "textarea")], async v => ctx.run("preferences", JSON.parse(v.get("preferences"))), "儲存偏好", ctx.error)));
  return el("div", {}, nodes);
}

function preferencesSection(ctx) {
  const prefs=ctx.data.preferences, snapshot=ctx.data.snapshot;
  const mine=snapshot?.teams.find(team=>team.id===snapshot.mine);
  const roster=[...new Set([...(mine?.players ?? []),...Object.keys(mine?.injury_players ?? {}),...prefs.untouchable])];
  const zones=[...new Set([prefs.timezone,"Asia/Taipei","America/New_York","UTC"])];
  return section("日常偏好", form([
    el("div",{class:"form-grid"},select("顯示時區","timezone",zones.map(zone=>[zone,zone]),prefs.timezone),field("保留加人次數","reserve_adds",prefs.reserve_adds,"number",{min:0,step:1,required:true})),
    disclosure("保護我的球員",el("p",{class:"muted"},"勾選的球員不會被列入可釋出名單。"),checks(ctx,roster,"untouchable",prefs.untouchable)),
    el("p",{class:"muted"},`目前每 ${number(prefs.sync_interval_seconds / 60,1)} 分鐘自動同步；排程與其他參數可在進階設定調整。`)
  ],async values=>ctx.run("preferences",{...prefs,timezone:values.get("timezone"),reserve_adds:Number(values.get("reserve_adds")),untouchable:values.getAll("untouchable")}),"儲存偏好",ctx.error));
}

function settingsSection(ctx) {
  const draft = ctx.data.state.draft;
  if (!draft) return section("聯盟設定", empty("同步 Yahoo 後會產生設定草稿，確認後才啟用計算。"));
  const changes = ctx.data.differences ?? [];
  return section("確認聯盟設定", el("p", { class: "warning" }, `請確認這些 Yahoo 未完整提供或需要核對的欄位：${draft.confirm_fields.join("、")}`), changes.length ? table(["項目", "本機值", "Yahoo 值"], changes.map(c => [c.key, JSON.stringify(c.local), JSON.stringify(c.yahoo)])) : el("p", { class: "muted" }, "Yahoo 設定與已確認的本機設定一致。"), form([field("設定草稿", "document", JSON.stringify(draft.document, null, 2), "textarea")], async v => ctx.run("settings", { document: JSON.parse(v.get("document")), decision: "import" }), "確認並匯入", ctx.error), ctx.data.state.league ? el("button", { onClick: () => ctx.run("settings", { document: {}, decision: "keep" }).catch(ctx.error) }, "保留本機設定") : null);
}

function sourcesSection(ctx) {
  const source = ctx.data.state?.sources ?? ctx.data.workspace?.sources ?? {};
  return section("球員資料與賽季前預測", el("p", {}, "目前尚未接上現成 NBA 供應商；此入口只接受專案自訂 PlayerSnapshot JSON。沒有符合格式的來源時，F1–F6 與球員對照都無法使用，即使不連 Yahoo 亦同。賽季前 forecast.json 以 SHA-256 鎖定版本。"), form([
    field("forecast.json 完整路徑", "forecast_path", source.forecast_path, "text", { required: true }), field("預測檔 SHA-256", "forecast_sha256", source.forecast_sha256, "text", { required: true, pattern: "[a-f0-9]{64}" }), field("預測公開時間（含時區）", "forecast_known_at", source.forecast_known_at, "text", { required: true, placeholder: "ISO 8601" }), field("已授權 NBA JSON 來源 HTTPS URL", "player_url", source.player_url, "url", { required: true }), field("來源授權依據", "permission_reference", source.permission_reference, "text", { required: true })
  ], async v => ctx.run("sources", Object.fromEntries(v)), "載入並鎖定來源", ctx.error));
}

export function teamsView(ctx) {
  if (!ctx.data.projection) return section("我的球隊", empty(ctx.data.projection_error ?? "請先在資料與設定載入球員資料。"));
  const scope=ctx.playerScope ?? (ctx.data.snapshot ? "mine" : "nba");
  const choices=[["mine","我的球隊"],["all","球員搜尋"],["nba","NBA 球隊與手調"]];
  const tabs=el("div",{class:"segmented",role:"group","aria-label":"球員檢視"},choices.map(([value,label])=>el("button",{"aria-pressed":String(scope===value),disabled:value==="mine"&&!ctx.data.snapshot,onClick:()=>{ctx.playerScope=value;ctx.render();}},label)));
  return el("div",{},tabs,scope==="nba" ? nbaTeamsView(ctx) : rosterView(ctx,scope));
}
function rosterView(ctx,scope) {
  const snapshot=ctx.data.snapshot;
  const mine=snapshot?.teams.find(team=>team.id===snapshot.mine);
  const owned=new Set([...(mine?.players ?? []),...Object.keys(mine?.injury_players ?? {})]);
  const details=new Map((ctx.data.team_views ?? []).flatMap(team=>team.players.map(player=>[player.player_id,player])));
  const owner=id=>snapshot?.teams.find(team=>team.players.includes(id)||Object.hasOwn(team.injury_players ?? {},id))?.name ?? (snapshot ? "自由球員" : "尚無聯盟名單");
  const search=field("搜尋球員","player_search",ctx.playerQuery ?? "","search",{placeholder:"姓名、球隊或位置",autocomplete:"off"});
  const filter=select("顯示","player_filter",[["all","全部球員"],["flags","需要確認"],["adjusted","已手動調整"]],ctx.playerFilter ?? "all");
  const body=el("div"),count=el("p",{class:"muted",role:"status"});
  function update() {
    const query=(ctx.playerQuery ?? "").trim().toLocaleLowerCase();
    const players=ctx.data.projection.players.filter(p=>(scope!=="mine"||owned.has(p.player.id))&&`${p.player.name} ${p.player.team_abbreviation ?? p.player.team_id} ${p.player.positions.join(" ")}`.toLocaleLowerCase().includes(query)&&(ctx.playerFilter!=="flags"||p.flags.length)&&(ctx.playerFilter!=="adjusted"||p.adjustments.length));
    count.textContent=`${players.length} 位球員 · 預測日期 ${ctx.data.projection.on}`;
    body.replaceChildren(players.length ? table(["球員", "歸屬", "出賽狀態", "出賽機率", "預測分鐘", "需要確認", "操作"],players.map(p=>[
      el("button",{class:"name",onClick:()=>ctx.player(p)},playerLabel(ctx,p.player.id)),owner(p.player.id),
      el("span",{class:details.get(p.player.id)?.manual_status ? "badge" : ""},details.get(p.player.id)?.manual_status ? `手調 ${details.get(p.player.id).manual_status}` : p.player.status),percent(p.probability),number(p.minutes,1),
      p.flags.length ? el("span",{class:"warning wrap-cell"},p.flags.map(flag=>flag.reason).join("；")) : "—",
      el("button",{class:"small",onClick:()=>ctx.edit(p)},"調整預測")
    ])) : empty(scope==="mine" && !owned.size ? "這個聯盟尚未讀取到你的名單，請同步後再查看。" : "沒有符合條件的球員，試著縮短關鍵字或切換篩選。"));
  }
  search.querySelector("input").addEventListener("input",event=>{ctx.playerQuery=event.target.value;update();});
  filter.querySelector("select").addEventListener("change",event=>{ctx.playerFilter=event.target.value;update();});
  update();
  return el("div",{},section(scope==="mine" ? mine?.name ?? "我的球隊" : "搜尋球員",el("div",{class:"filter-bar"},search,filter),count,body),disclosure("手動調整紀錄",ledgerSection(ctx)));
}
function nbaTeamsView(ctx) {

  const projection = ctx.data.projection;
  if (!projection) return section("球隊與有效預測", empty(ctx.data.projection_error ?? "先在資料與同步頁載入賽季前預測與 NBA 資料來源。"));
  const summaries = ctx.data.team_views;
  if (!summaries.length) return empty("尚無 NBA 球隊預測，請確認資料來源。");
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
    return [el("button", { class: "name", onClick: () => ctx.player(p) }, playerLabel(ctx,p.player.id)), el("span", {class: detail.manual_status ? "adjusted" : "", title:adjustments}, detail.manual_status ? `${p.player.status} → 手調 ${detail.manual_status}` : p.player.status), detail.manual_return_on ? `手調 ${detail.manual_return_on} · 來源 ${detail.return_on ?? "未提供"}` : detail.return_on ?? "—", number(p.traces.minutes.result, 1), el("button", { class: p.adjustments.some(e => ctx.data.parameters.fields.find(f => f.id === e.field)?.targets.includes("minutes")) ? "small adjusted" : "small", onClick: () => ctx.edit(p), title: adjustments }, number(p.minutes, 1)),
      ...counts.map(n => detail.recent_minutes[n] ? el("div", {}, number(detail.recent_minutes[n].result, 1), formula(detail.recent_minutes[n], ctx.data.formulas)) : "—"),
      ...multiplierFields.map(f => detail.multipliers[f.id] ? el("span", {class:"adjusted", title:adjustments}, number(detail.multipliers[f.id].result), formula(detail.multipliers[f.id], ctx.data.formulas)) : "1"),
      owner(p.player.id), el("span", { class: "warning" }, p.flags.length ? p.flags.map(f => f.reason).join("；") : "—")];
  });
  const flags = projection.players.flatMap(p => p.flags.map(f => [
    el("button", {class:"name",onClick:()=>ctx.player(p)}, p.player.name), p.player.team_id, f.reason,
    `${number(f.model)} → ${number(f.observed)}`, el("div",{class:"actions"},
      el("button",{class:"small",onClick:()=>ctx.edit(p,f)},"建立手調"),
      el("button",{class:"small",onClick:()=>ctx.ignore(f)},"暫時忽略"))
  ]));
  const content = el("div", {}, section("需要你確認", flags.length ? table(["球員","球隊","理由","模型／觀察","處理"],flags) : empty("目前沒有未忽略的模型旗標。來源與 Yahoo 設定問題請看資料與同步頁。")), section(selected,
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
    el("div",{class:"player-overview"},avatar(player.player.name,true),el("p",{},`${player.player.team_abbreviation ?? player.player.team_id} · ${player.player.positions.join(" / ")}`),el("button",{class:"primary",onClick:()=>ctx.edit(player)},"調整預測")),
    el("p", {}, `${player.player.team_id} · ${player.player.positions.join(" / ")} · 出賽 ${percent(player.probability)} · 有效分鐘 ${number(player.minutes, 1)}`),
    el("p", {}, `分鐘手調：${manual("minutes")} · 出賽手調：${manual("q")}`),
    minutesChart(player, ctx.data.parameters.fields), formula(player.traces.minutes, ctx.data.formulas),
    player.adjustments.map(entry => {
      const prefix = `adjustment:${entry.id}:`;
      const applied = Object.entries(player.traces).filter(([key]) => key.startsWith(prefix));
      return applied.length ? el("details", {}, el("summary", {}, `手調計算：${entry.field} · ${entry.reason}`), applied.map(([key, trace]) => el("div", {}, el("p", {class:"muted"}, key.slice(prefix.length)), formula(trace, ctx.data.formulas)))) : null;
    }),
    el("p", {class:"muted"}, "投籃命中數的混合值保留原始估計；右側最終每分鐘值按手調後出手 × 手調後命中率重算，模擬使用最終值。"),
    disclosure("逐項預測、先驗與完整算式",table(["每分鐘數據／命中率", "先驗", "當季", "混合", "當季權重", "k", "手調", "最終值", "算式"], [...rows, ...shots])),
    player.flags.map(f => el("div", { class: "flag" }, f.reason, el("p", {}, `模型 ${number(f.model)} · 觀察 ${number(f.observed)}`), (f.traces ?? []).map(t => formula(t, ctx.data.formulas)), el("button", { class: "small", onClick: () => ctx.edit(player, f) }, "建立手調"), el("button", { class: "small", onClick: () => ctx.ignore(f) }, "暫時忽略"))),
    el("p", {}, `來源 ${ctx.data.player_source?.source ?? "—"} · 資料時間 ${dateText(ctx.data.player_source?.as_of, ctx.data.preferences.timezone)}`),
    el("p", { class: "hash" }, `球員快照 ${ctx.data.state?.players_sha256 ?? ctx.data.workspace?.players_sha256} · 預測快照 ${ctx.data.state?.priors_sha256 ?? ctx.data.workspace?.priors_sha256}`));
}

export function weekView(ctx) {
  const day = ctx.data.projection?.on ?? new Date().toLocaleDateString("en-CA");
  const weeks = ctx.data.state.league.matchups.filter(w => w.end >= day);
  const result = ctx.results.week;
  const selector = form([select("對戰週", "week_id", weeks.map(w => [w.id, `${w.id} · ${w.start} — ${w.end}`]), result?.week_id)], async v => { ctx.results.week = await ctx.run("week", Object.fromEntries(v)); ctx.render(); }, "計算對戰", ctx.error);
  selector.className = "inline-form";
  const nodes = [selector];
  if (!result) return el("div", {}, nodes, empty("選擇對戰週，計算已打完的實際值與剩餘合法先發。"));
  nodes.push(forecastCard(ctx, result));
  nodes.push(section("自由球員計畫", el("p", { class: "muted" }, "假設對手名單不變；所有加人與丟人都由你在 Yahoo 操作。"), !ctx.data.calibration.enabled ? el("p", { class: "warning" }, ctx.data.calibration.reason) : null, el("button", { class: "primary", onClick: async () => { try { ctx.results.plans = await ctx.run("recommendations", { week_id: result.week_id }); ctx.render(); } catch (e) { ctx.error(e); } } }, "搜尋換人建議"), ctx.results.plans?.length === 0 ? empty("目前沒有同時符合本週目標與整季保護規則的換人計畫。") : null, (ctx.results.plans ?? []).slice(0, 10).map(plan => el("section", { class: "plan" }, el("h3", {}, plan.moves.map(m => `${m.effective_on}：${playerName(ctx, m.add)} ↔ ${playerName(ctx, m.drop)}`).join("；")), planValue(plan), plan.traces.map(t => formula(t, ctx.data.formulas)), table(["加人", "可先發場次"], plan.moves.map(m => [playerName(ctx, m.add), m.starter_games])), tradeCategories(ctx, "本週各類別勝率前後", [plan.before], [plan.after], plan.category_changes), el("button", { onClick: () => { ctx.results.selectedPlan = plan.id; delete ctx.results.today; ctx.navigate("today"); } }, "放入今日待辦"), el("label", {class:"checklist"}, el("input", {type:"checkbox", checked:ctx.data.notes?.adopted?.[plan.id] === true, onChange:e => ctx.run("adopt", {id:plan.id, completed:e.target.checked}).catch(ctx.error)}), "我已在 Yahoo 採納這個計畫")))));
  return el("div", {}, nodes);
}

function forecastCard(ctx, result) {
  const opponent = ctx.data.snapshot?.teams.find(t=>t.id===result.away)?.name ?? result.away;
  const strategy = {safe:"維持優勢", key:"關鍵類別", abandon:"暫不優先"};
  const value = (category, side) => category[`${side}_denominator`] === null ? number(category[side]) : `${number(category[`${side}_numerator`])} / ${number(category[`${side}_denominator`])} = ${number(category[side])}`;
  return section("這週怎麼贏",
    el("div", {class:"scoreline"}, el("div", {}, el("p", {class:"eyebrow"}, `VS. ${opponent}`), el("strong", {class:"large-number"}, scoreText(result.score, result.scoring)), el("p", {class:"muted"}, result.scoring === "h2h_one_win" ? "預測整週勝率" : "預期贏的類別數")), el("div", {class:"matchup-meta"}, el("p", {}, `剩餘可先發 ${result.remaining_games?.[result.home] ?? "未知"} 場 / 對手 ${result.remaining_games?.[result.away] ?? "未知"} 場`), el("p", {}, `剩餘加人 ${result.adds_remaining ?? "未知"} · 已過 ${result.elapsed_days} 天`))),
    matchupPolicy(result.priority),
    el("p", {}, `維持優勢：${result.categories.filter(c=>c.strategy==="safe").map(c=>c.label).join("、") || "目前沒有"} · 爭取關鍵類別：${result.categories.filter(c=>c.strategy==="key").map(c=>c.label).join("、") || "目前沒有"} · 暫不優先投入：${result.categories.filter(c=>c.strategy==="abandon").map(c=>c.label).join("、") || "目前沒有"}`),
    el("p", {class:"muted"}, result.lineup_search === "joint_exact" ? "我方本週合法排陣已完整搜尋；對手維持固定基準排陣。" : "每日排陣使用精確窮舉；整週採逐日反覆改善，不保證聯合全域最優。"),
    result.prior_players?.length ? el("p",{class:"warning"}, `無逐場歷史，以先驗抽樣：${result.prior_players.map(p=>playerName(ctx,p)).join("、")}；不確定性尚未經真實 holdout 校準。`) : null,
    result.injury_returns?.length ? table(["IL 回歸假設","生效日","必要丟人","日期依據"],result.injury_returns.map(m=>[
      playerName(ctx,m.player_id),m.effective_on,m.drop ? playerName(ctx,m.drop) : "有空名額",m.estimated ? "來源估計，仍可能更改" : "最新狀態已不符 IL 資格"
    ])) : null,
    table(["類別", "我方預測總量", "對手預測總量", "調整後勝率", "策略"], result.categories.map(c => [c.label, value(c,"home"), value(c,"away"), percent(c.probability), el("span", {class:`badge strategy-${c.strategy}`}, strategy[c.strategy])])),
    disclosure("預測依據與完整算式",
      el("p", {}, `整週未校準值 ${scoreText(result.raw_score,result.scoring)} · 不另加退／不交易基準 ${scoreText(result.no_moves_score,result.scoring)}（保留手調與 IL 回歸情境、最佳化每日排陣）`),
      el("p", {class:"muted"}, `模擬 ${result.simulations} 次 · 誤差 ±${number(result.standard_error,4)} · 整週校準 c=${result.scoring === "h2h_one_win" ? ctx.data.parameters.week_calibration.value : ctx.data.parameters.calibration.value} · 類別 c=${ctx.data.parameters.calibration.value}`),
      table(["類別", "原始勝率", "調整後", "核對計算"], result.categories.map(c => [c.label,percent(c.raw_probability),percent(c.probability),el("div", {},c.traces.map(t=>formula(t,ctx.data.formulas)),c.value_traces?.length ? disclosure("我方／對手總量的計算",el("p",{class:"muted"},`資料列：我方、對手；模擬平均數據欄位：${c.value_axes.join("、")}`),c.value_traces.map(t=>formula(t,ctx.data.formulas))) : null)])),
      result.traces.map(t=>formula(t,ctx.data.formulas)),jsonDetails("逐日合法先發",result.lineups)));
}

function matchupPolicy(priority) {
  return el("p", {class:priority?.status === "must_win" ? "warning" : "muted"},
    priority?.reason ?? "尚無對戰重要性判定；保留整季強度。",
    priority?.status === "must_win" ? " 本週勝率優先，可犧牲長期價值。" : " 換人建議須通過剩餘賽季強度不下降檢查。"
  );
}


function planValue(plan) {
  return el("div", {},
    el("p", {}, `整份計畫：本週 Δ ${number(plan.delta_week)} · 分數 ${number(plan.score)}`),
    plan.priority?.status === "must_win"
      ? el("p", {class:"warning"}, "淘汰風險已確認：此計畫只比較本週增益，未重新計算長期損益。")
      : el("p", {class:plan.delta_strength == null ? "warning" : "muted"},
        plan.delta_strength == null ? (plan.strength_unavailable ?? "剩餘賽季強度尚無法確認。")
          : `剩餘例行賽 Δ ${number(plan.delta_season)} · 含季後賽的剩餘賽季 Δ ${number(plan.delta_strength)}（須不下降）`)
  );
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
  const sorts = el("div", {class:"actions", role:"group", "aria-label":"交易結果排序"}, el("span", {class:"muted"}, "結果排序"), [["expected_gain", "期望值"], ["mine_delta", "增益最大"], ["acceptance", "最可能成交"]].map(([key, label]) => el("button", {"aria-pressed": String(sort === key), disabled: !ctx.results.trades?.length || ctx.pending, onClick: () => {ctx.tradeSort = key; ctx.render();}}, label)));
  const feedback = ctx.jobFeedback && ["trade-search", "partners", "preferences"].includes(ctx.jobFeedback.action) ? ctx.jobFeedback : null;
  const searchFeedback = el("p", {class:feedback?.state === "failed" ? "negative" : "muted", role:"status", "aria-live":"polite", "data-job-actions":"trade-search,partners,preferences", "aria-busy":String(feedback?.state === "running")}, feedback?.message ?? "選擇上方的搜尋方式，結果會列在下方。");
  const rankedTrades = [...(ctx.results.trades ?? [])].sort((a,b) => (a[sort] === null)-(b[sort] === null) || Math.round((b[sort] ?? 0)/ctx.data.parameters.tolerance.value) - Math.round((a[sort] ?? 0)/ctx.data.parameters.tolerance.value) || a.opponent.localeCompare(b.opponent) || a.send.join().localeCompare(b.send.join()) || a.receive.join().localeCompare(b.receive.join()));
  const audit = ctx.results.tradeAudit;
  const searchStatus = audit ? el("div", {},
    el("p", {class:"muted"}, `價值門檻 ${percent(ctx.results.tradeRatio)} · 原組合 ${audit.candidates} · 價值差過大 ${audit.value_filtered} · 進入比較 ${audit.eligible} · 完整評估 ${audit.full_effects}`),
    audit.unknown_value ? el("p", {class:"warning"}, `${audit.unknown_value} 組缺少公開排名，無法判斷交易價值，已略過；仍可手動評估。`) : null,
    ctx.results.trades.length === 0 ? empty("目前沒有符合搜尋條件的合法交易。") : null
  ) : null;
  return el("div", {}, section("交易分析", manual), trade ? tradeCard(ctx, trade) : null, section("尋找交易機會", search, disclosure("調整交易價值範圍", tradeValueControl(ctx)), searchFeedback, searchStatus, sorts, !ctx.results.trades?.length ? el("p", {class:"muted"}, "取得搜尋結果後，才能切換排序。") : el("div", {}, el("p", {class:"muted"}, "點選期望值，可用目前資料查看完整交易評估。"), table(["對象", "送出", "收到", "我方增益", "對方 ΔN", "接受率", "期望值"], rankedTrades.slice(0, 30).map(t => [t.opponent, t.send.map(p => playerName(ctx, p)).join("、"), t.receive.map(p => playerName(ctx, p)).join("、"), number(t.mine_delta), number(t.opponent_delta), percent(t.acceptance), el("button", { class: "small", onClick: () => openSearchedTrade(ctx, t) }, number(t.expected_gain))])))), disclosure(`提案紀錄（${(ctx.data.proposals ?? []).length}）`,proposalsSection(ctx)));
}
async function openSearchedTrade(ctx, trade) {
  try {
    const details = await ctx.run("trade", {opponent:trade.opponent, send:trade.send, receive:trade.receive});
    ctx.open(tradeCard(ctx, details));
  } catch (error) { ctx.error(error); }
}
function tradeValueControl(ctx) {
  const limits = ctx.data.preference_controls.trade_value_min_ratio;
  const control = field("交易價值範圍", "trade_value_min_ratio", Math.round((ctx.tradeValueRatio ?? ctx.data.preferences.trade_value_min_ratio) * 100), "range", {min:limits.minimum * 100, max:limits.maximum * 100, step:1, dir:"rtl"});
  const explanation = el("p");
  const input = control.querySelector("input");
  const describe = () => { ctx.tradeValueRatio = Number(input.value) / 100; explanation.textContent = `目前 ${input.value}%：一方估值 100，另一方至少 ${input.value}，才進一步比較隊伍適配。`; };
  input.addEventListener("input", describe); describe();
  return form([control, el("p", {class:"muted"}, `← 嚴格 ${limits.maximum * 100}%　·　寬鬆 ${limits.minimum * 100}% →`), explanation, el("p", {class:"muted"}, `預設 ${limits.default * 100}%。依公開排名估值，先排除價值差太大的交易；這是搜尋範圍，不是對方接受率。多換多比較整包價值。下次搜尋會自動儲存目前範圍。`)], async v => {
    await ctx.run("preferences", {...ctx.data.preferences, trade_value_min_ratio:Number(v.get("trade_value_min_ratio")) / 100});
    ctx.tradeValueRatio = null;
  }, "儲存搜尋範圍", ctx.error);
}
async function searchTrade(ctx, opponent, size) {
  try {
    if (ctx.tradeValueRatio != null && ctx.tradeValueRatio !== ctx.data.preferences.trade_value_min_ratio) {
      await ctx.run("preferences", {...ctx.data.preferences, trade_value_min_ratio:ctx.tradeValueRatio});
      ctx.tradeValueRatio = null;
    }
    const result = await ctx.run("trade-search", { opponent, size });
    ctx.results.trades = result.trades; ctx.results.tradeAudit = result.counts; ctx.results.tradeRatio = result.minimum_value_ratio;
    ctx.render();
  } catch (e) { ctx.error(e); }
}
function tradeCategories(ctx, label, before, after, changes) {
  return el("details", {}, el("summary", {}, label), table(["週", "類別", "交易前", "交易後", "差異"], before.flatMap(w => {
    const changed = after.find(a => a.week_id === w.week_id);
    return w.categories.map(c => {const next = changed.categories.find(n => n.id === c.id); return [w.week_id, c.label, percent(c.probability), percent(next.probability), changes?.[w.week_id]?.[c.id] ? formula(changes[w.week_id][c.id], ctx.data.formulas) : "—"];});
  })));
}
function tradeCard(ctx, trade) {
  return section("交易評估", el("p", { class: "large-number" }, `${number(trade.mine_delta)} 勝`), el("p", {}, `季後賽機率 Δ ${percent(trade.playoff_delta)} · 季後賽場次 Δ ${number(trade.playoff_games_delta)}`), el("p", {}, `對方公開價值 ΔR ${number(trade.rank_delta)} · 需求 ΔN ${number(trade.opponent_delta)} · 接受率 ${percent(trade.acceptance)} `, el("span", { class: "badge" }, trade.calibrated ? "已校正" : "未校正")), trade.acceptance_unavailable ? el("p", {class:"warning"}, trade.acceptance_unavailable) : null, trade.traces.map(t => formula(t, ctx.data.formulas)), tradeCategories(ctx, "我方各週類別勝率前後", trade.before, trade.after, trade.category_changes), tradeCategories(ctx, "對方各週類別勝率前後", trade.opponent_before, trade.opponent_after, trade.opponent_category_changes), jsonDetails("自動補人／丟人與交易後名單", { adds: trade.automatic_adds, drops: trade.automatic_drops, rosters: trade.rosters }), el("button", { disabled: trade.acceptance === null, onClick: () => ctx.run("proposal", { opponent: trade.opponent, send: trade.send, receive: trade.receive, outcome: "pending", supersedes: null }).catch(ctx.error) }, "記錄我已在 Yahoo 提案"));
}
function proposalsSection(ctx) {
  return section("提案紀錄", el("p", { class: "muted" }, "只記錄你手動提出的交易，不會替你送出。"), table(["日期", "對象", "送出", "收到", "預測機率", "結果", "更新"], (ctx.data.proposals ?? []).map(p => [dateText(p.created_at, ctx.data.preferences.timezone), p.opponent, p.send.map(id => playerName(ctx, id)).join("、"), p.receive.map(id => playerName(ctx, id)).join("、"), percent(p.probability), p.outcome, el("div", { class: "actions" }, ["accepted", "rejected", "withdrawn"].map(outcome => el("button", { class: "small", onClick: () => ctx.run("proposal", { opponent: p.opponent, send: p.send, receive: p.receive, outcome, supersedes: p.id }).catch(ctx.error) }, { accepted: "接受", rejected: "拒絕", withdrawn: "撤回" }[outcome])))])), el("button", { onClick: async () => { try { const fit = await ctx.run("refit-acceptance", {}); ctx.open(section("接受模型擬合報告", el("p", {}, `${fit.count} 筆提案 · Log loss ${number(fit.log_loss, 6)} · 訓練樣本內評估`), (fit.traces ?? []).map(t => formula(t, ctx.data.formulas)), calibrationChart(fit.bins), table(["機率區間", "筆數", "平均預測", "實際", "偏差", "算式"], fit.bins.map(b => [`${percent(b.lower)} — ${percent(b.upper)}`, b.count, percent(b.predicted), percent(b.observed), percent(b.difference), el("div", {}, (b.traces ?? []).map(t => formula(t, ctx.data.formulas)))])), jsonDetails("係數與各區間", fit))); } catch (e) { ctx.error(e); } } }, "用已結案提案重新擬合"));
}

export function todayView(ctx) {
  const result = ctx.results.today;
  const selector = form([field("日期", "on", result?.on ?? ctx.data.projection.on, "date", {required:true})], async v => {
    ctx.results.today = await ctx.run("today", {on:v.get("on"), plan_id:ctx.results.selectedPlan ?? null});
    ctx.render();
  }, result ? "重新計算安排" : "計算今日安排", ctx.error);
  selector.className = "inline-form";
  if (!result) return el("div", {},
    el("div", {class:"welcome"}, el("p", {class:"eyebrow"}, "MAKE EVERY GAME COUNT"), el("h2", {}, "先把今天的陣容排好。"),
      el("p", {}, "依你的名單、球員開賽時間與本週對戰目標，整理今天要做的事。"),
      ctx.results.selectedPlan ? el("p", {class:"badge"}, "已帶入本週換人計畫，計算後查看安排。") : null, selector),
    el("div", {class:"next-steps"}, el("button", {onClick:()=>ctx.navigate("week")}, "查看本週對戰 →"), el("button", {onClick:()=>ctx.navigate("teams")}, "查看球員與預測 →")));
  const actions = result.actions.map(a => el("li", {class:a.completed ? "completed" : ""},
    el("label", {}, el("input", {type:"checkbox", checked:a.completed, onChange:async event => {
      const checked = event.target.checked;
      event.target.disabled = true;
      try { await ctx.run("complete", {id:a.id, completed:checked}); a.completed = checked; ctx.render(); }
      catch (error) { event.target.checked = a.completed; ctx.error(error); }
      finally { event.target.disabled = false; }
    }}), playerLabel(ctx,a.player_id), el("span", {class:"task-destination"}, a.slot ? slotText(ctx,a.slot) : ({add:"加入名單",drop:"釋出球員",activate:"移出傷兵席",bench:"板凳",start:"先發"}[a.kind] ?? a.kind))),
    el("p", {class:"task-reason"}, a.reason)));
  const scoring = ctx.data.state.league.scoring;
  return el("div", {}, selector,
    result.injury_pending?.length ? el("p", {class:"flag"}, `IL 回歸待確認或尚缺可沿用的釋出計畫：${result.injury_pending.map(p=>playerName(ctx,p)).join("、")}。目前排陣與本週預測不含這些啟用；請先確認最新傷病狀態，必要時到本週頁計算釋出計畫。`) : null,
    result.recommendation?.moves.some(m=>m.effective_on<=result.on) ? el("p", {class:"flag"}, "以下排陣以完成這份換人計畫為前提；請先在 Yahoo 換人，再同步確認名單。") : null,
    section("今天要做的事", el("div", {class:"task-summary"},
      el("p", {}, el("strong", {}, `${result.actions.filter(a=>!a.completed).length} 項待辦`), " · 勾選代表你已在 Yahoo 完成操作。"),
      el("p", {class:"muted"}, `本週預測 ${scoreText(result.score_before,scoring)} → ${scoreText(result.score_after,scoring)}`)),
      actions.length ? el("ul", {class:"checklist task-list"}, actions) : empty("今天沒有需要調整的位置。請留意開賽前的傷病消息。"),
      disclosure("這份安排的計算依據", result.traces.map(t=>formula(t,ctx.data.formulas)))),
    section("今日名單與開賽時間", table(["球員", "對手／開賽", "狀態", "建議位置", "決策依據"], result.players.map(p => [
      playerLabel(ctx,p.player_id), el("div", {}, p.opponents.join("、") || "無賽程", el("small", {}, p.tipoffs.map(t=>dateText(t,ctx.data.preferences.timezone)).join("、") || "—")),
      p.status, slotText(ctx,p.slot), disclosure(p.reason,
        p.marginal ? el("div", {}, formula(p.marginal,ctx.data.formulas), Object.entries(p.category_traces ?? {}).map(([label,traces])=>disclosure(`${label} Δ ${number(p.category_changes[label])}`,traces.map(t=>formula(t,ctx.data.formulas))))) : "尚無影響計算")
    ])),
      disclosure("查看各球員鎖定時間", table(["球員", "鎖定時間", "距離鎖定"], Object.entries(result.locks).map(([p,lock])=>[
        playerName(ctx,p),dateText(lock,ctx.data.preferences.timezone),new Date(lock)<=new Date() ? "已鎖定" : `${number((new Date(lock)-new Date())/60000,0)} 分鐘`
      ])))),
    result.week_forecast ? forecastCard(ctx,result.week_forecast) : matchupPolicy(result.priority),
    result.recommendation ? section("已計算的換人計畫", planValue(result.recommendation),
      table(["生效日", "加入", "釋出"],result.recommendation.moves.map(m=>[m.effective_on,playerLabel(ctx,m.add),playerLabel(ctx,m.drop)])),
      disclosure("釋出風險與完整依據", table(["釋出球員", "Yahoo 持有率", "走勢", "資料時間"],[...new Set(result.recommendation.moves.map(m=>m.drop))].map(id=>{
        const owned=ctx.data.snapshot?.ownership?.[id];
        return [playerName(ctx,id),owned?.value==null ? "未知" : percent(owned.value),owned?.change==null ? "未知" : percent(owned.change),dateText(owned?.as_of,ctx.data.preferences.timezone)];
      })),result.recommendation.traces.map(t=>formula(t,ctx.data.formulas))))
      : el("div", {class:"next-steps"}, el("p", {class:"muted"}, "還沒有已計算的換人計畫。"),el("button", {onClick:()=>ctx.navigate("week")}, "到本週對戰搜尋換人 →")));
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
    el("p", {class:cumulative.refit_alert ? "warning" : "muted"}, cumulative.refit_alert ? "累積偏差超過門檻與不確定性範圍，請檢查校準。" : `每個分箱至少需 ${ctx.data.parameters.calibration_minimum.value} 個獨立對戰週，且偏差需超過門檻加誤差範圍；樣本不足不能代表已校準。`),
    table(["週數", "類別預測筆數", "類別 Brier", "整週 Brier"], [[cumulative.weeks,
      cumulative.category_predictions, number(cumulative.category_brier), number(cumulative.week_brier)]]),
    cumulative.unverified_rule_weeks ? el("p", {class:"warning"}, `${cumulative.unverified_rule_weeks} 週缺少已驗證的歷史聯盟規則；以上統計包含這些相容讀取的結果，不能確認計分規則與當時一致。`) : null,
    cumulative.excluded_legacy_weeks ? el("p", {class:"muted"}, `${cumulative.excluded_legacy_weeks} 週舊版積分預測保留原評分，未混入純勝率的整週 Brier。`) : null,
    (cumulative.traces ?? []).map(t => formula(t, ctx.data.formulas)), calibrationChart(cumulative.bins), table(["機率區間", "類別筆數", "獨立週數", "有效週數", "預測", "實際", "偏差", "誤差範圍", "算式"], cumulative.bins.map(b => [
      `${percent(b.lower)} — ${percent(b.upper)}`, b.count, b.independent_samples, number(b.effective_samples, 2), percent(b.predicted), percent(b.observed), percent(b.difference), percent(b.uncertainty), el("div", {}, (b.traces ?? []).map(t => formula(t, ctx.data.formulas)))
    ]))) : null;
  return el("div", {}, summary, download, section("每週回顧與模型監控", el("p", {}, "使用當時已保存的預測紀錄對照 Yahoo 最終結果；事後變更資料不會改寫過去預測。")), reports.length ? reports.map(r => section(`對戰週 ${r.week_id}`, r.rules_verified === true ? null : el("p", {class:"warning"}, "歷史聯盟規則未確認：部分原始預測未保存當時規則，或這份舊報告未記錄規則驗證。以下結果僅供相容讀取，不能確認計分規則與當時一致。"), el("p", { class: r.refit_alert ? "warning" : "muted" }, r.refit_alert ? "校準偏離超過門檻，請重新擬合校準係數。" : "本週獨立樣本不足，校準偏離請看累積監控。"), table(["類別 Brier", r.week_score_kind === "win_probability" ? "整週勝率 Brier" : "舊版積分 Brier", "有手調誤差", "無手調誤差"], [[number(r.category_brier), number(r.week_brier), number(r.with_adjustments_mae), number(r.without_adjustments_mae)]]), calibrationChart(r.bins), table(["機率區間", "筆數", "平均預測", "實際", "偏差", "算式"], r.bins.map(b => [`${percent(b.lower)} — ${percent(b.upper)}`, b.count, percent(b.predicted), percent(b.observed), percent(b.difference), el("div", {}, (b.traces ?? []).map(t => formula(t, ctx.data.formulas)))])), r.traces.map(t => formula(t, ctx.data.formulas)), jsonDetails("逐項預測", r.rows), table(["計畫", "採納狀態", "當時預測增益", "該週實際得分"], r.recommendation_outcomes.map(o => [o.plan_id, o.adopted === true ? "已採納" : o.adopted === false ? "未採納" : "未標記", number(o.predicted_gain), number(o.actual_week_score)])), el("p", {class:"muted"}, "實際得分反映整週結果；未採納計畫沒有已觀察到的反事實增益。"))) : empty("尚無已結束且有事前預測紀錄的對戰週。每週最終同步後會自動產生報告。"));
}
