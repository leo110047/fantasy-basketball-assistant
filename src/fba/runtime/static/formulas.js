// Both applications render the same server-owned definition and calculation trace.
function element(tag, text = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}
function numeric(value) {
  return Array.isArray(value) ? `[${value.map(numeric).join(", ")}]` : String(value);
}
export function formula(trace, definitions) {
  const definition = definitions.find(item => item.id === trace.formula_id);
  const container = element("details");
  container.className = "formula";
  container.append(element("summary", `${definition?.name ?? trace.formula_id} · 展開算式`));
  if (!definition) {
    container.append(element("p", `公式登錄缺失：${trace.formula_id}`));
    return container;
  }
  container.append(element("code", definition.latex));
  const table = element("table"), header = element("tr"), body = element("tbody");
  for (const title of ["代入值", "數字", "單位"]) header.append(element("th", title));
  const head = element("thead"); head.append(header); table.append(head, body);
  for (const [key, value] of Object.entries(trace.inputs)) {
    const row = element("tr");
    for (const text of [key, numeric(value), definition.input_units[key]]) row.append(element("td", text));
    body.append(row);
  }
  container.append(table, element("p", `結果 ${numeric(trace.result)} · ${definition.units}`));
  if (definition.parameters.length) container.append(element("p", `參數：${definition.parameters.join(", ")}`));
  return container;
}
