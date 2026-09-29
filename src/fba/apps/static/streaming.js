import {node} from "/view.js";

export function renderStreamingResult(container, value, players) {
  container.append(node("p", `建議比較策略：${value.recommended_slots} 格 · 目前停止價採 ${value.configured_slots} 格。這次分析不會自動更改停止價策略。`));
  container.append(node("p", `${value.health_samples} 條健康路徑、${value.health_blocks} 組、${value.matchup_count} 個對戰期；共用 ${value.pool_size} 位自由球員。每個設定加人期間合計最多 ${value.shared_limit} 次，保留 ${value.reserve_adds} 次補傷兵。`, "muted"));
  container.append(node("p", `以下是每個對戰期的平均；對戰期可能跨多週，加人上限依聯盟的加人期間重設。0 格仍可補傷兵${value.upgrades ? "與永久升級" : "；永久升級已關閉"}。`, "muted"));
  const scroll = node("div", "", "table-scroll"), table = node("table", ""), header = node("tr", "");
  for (const text of ["串流格", "先發場次", "傷兵加人", "永久升級", "串流加人", "相對 0 格增益"]) header.append(node("th",text));
  const head = node("thead", ""); head.append(header); table.append(head);
  const body = node("tbody", "");
  for (const row of value.scenarios) {
    const tr = node("tr", "");
    const cells = [String(row.slots), ...[row.started,row.injury_adds,row.upgrade_adds,row.stream_adds].map(n => n.toFixed(2)), row.gain.toFixed(6)];
    for (const value of cells) tr.append(node("td",value));
    body.append(tr);
  }
  table.append(body); scroll.append(table); container.append(scroll);
  for (const row of value.comparisons) container.append(node("p", `${row.slots} 對 ${row.versus} 格：分組增益 ${row.block_minimum.toFixed(6)} 至 ${row.block_maximum.toFixed(6)} · ${row.accepted ? "每組均改善" : "保留較少格數"}`, "muted"));
  if (value.flex.length) container.append(node("p", `可操作候選（移除損失由小到大）：${value.flex.map(p => `${players.get(p.player_id)?.name ?? p.player_id}（${p.removal_loss.toFixed(6)}）`).join("、")}。不是立即丟棄指令。`));
  if (value.flex.length < value.recommended_slots) container.append(node("p", "目前自由球員池不足以列出所有格數的合法替換候選。", "warning"));
  container.append(node("p", "增益與移除損失是固定局部梯度的模型效用，不是美元或勝率。只有全部健康分組都改善才增加格數；結論受健康、自由球員可得性與對手管理假設影響。", "muted"));
}
