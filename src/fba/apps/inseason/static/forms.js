export {formula} from "/formulas.js";
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "checked" || key === "disabled") node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child !== null && child !== undefined) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
export function field(label, name, value = "", type = "text", options = {}) {
  const input = el(type === "textarea" ? "textarea" : "input", { name, ...options });
  if (type !== "textarea") input.type = type;
  input.value = value ?? "";
  return el("label", {}, label, input);
}
export function select(label, name, choices, value) {
  const input = el("select", { name }, choices.map(([key, title]) => el("option", { value: key }, title)));
  if (value !== undefined) input.value = value;
  return el("label", {}, label, input);
}
export function form(children, submit, label = "儲存", onError = () => {}) {
  const node = el("form", {}, children, el("div", { class: "actions" }, el("button", { type: "submit", class: "primary" }, label)));
  node.addEventListener("submit", async event => {
    event.preventDefault();
    const button = node.querySelector('button[type="submit"]');
    button.disabled = true;
    try { await submit(new FormData(node), node); } catch (error) { onError(error); }
    finally { button.disabled = false; }
  });
  return node;
}
export function table(headers, rows) {
  return el("div", { class: "table-scroll" }, el("table", {}, el("thead", {}, el("tr", {}, headers.map(h => el("th", { scope: "col" }, h)))), el("tbody", {}, rows.map(row => el("tr", {}, row.map(cell => el("td", {}, cell)))))));
}
export function number(value, digits = 3) {
  return value === null || value === undefined ? "—" : new Intl.NumberFormat("zh-TW", { maximumFractionDigits: digits }).format(value);
}
export const percent = value => value === null || value === undefined ? "—" : `${number(value * 100, 1)}%`;
export function empty(message) { return el("p", { class: "empty" }, message); }
export function minutesChart(player, fields) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 800 170"); svg.setAttribute("class", "minutes");
  svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "逐場分鐘與模型分鐘走勢");
  const values = player.observed_minutes;
  const manual = player.adjustments.filter(a => fields.find(f => f.id === a.field)?.targets.includes("minutes"));
  const dates = [...player.observed_dates, ...manual.flatMap(a => [a.starts_on, a.ends_on])].map(d => Date.parse(d));
  const first = Math.min(...dates), last = Math.max(...dates);
  const x = d => 20 + (Date.parse(d) - first) / Math.max(86400000, last - first) * 760;
  const maximum = Math.max(1, ...values, player.minutes, player.traces.minutes.result, ...manual.map(a=>a.value));
  const y = value => 150 - value / maximum * 125;
  for (const adjustment of manual) {
    const band = document.createElementNS(ns, "rect");
    for (const [k,v] of Object.entries({x:x(adjustment.starts_on), y:20, width:Math.max(2,x(adjustment.ends_on)-x(adjustment.starts_on)), height:130, fill:"#dba849", opacity:0.18})) band.setAttribute(k,v);
    const title = document.createElementNS(ns,"title"); title.textContent=`手調 ${adjustment.value} 分鐘 · ${adjustment.starts_on} 至 ${adjustment.ends_on} · ${adjustment.reason}`;
    band.append(title); svg.append(band);
    const adjustedLine=document.createElementNS(ns,"line");
    for (const [k,v] of Object.entries({x1:x(adjustment.starts_on),x2:Math.max(x(adjustment.starts_on)+2,x(adjustment.ends_on)),y1:y(adjustment.value),y2:y(adjustment.value),stroke:"#9c6a08","stroke-width":3})) adjustedLine.setAttribute(k,v);
    svg.append(adjustedLine);
  }
  const line = document.createElementNS(ns, "line");
  for (const [k, v] of Object.entries({ x1: 20, x2: 780, y1: y(player.traces.minutes.result), y2: y(player.traces.minutes.result), class: "model" })) line.setAttribute(k, v);
  svg.append(line);
  values.forEach((value, i) => {
    const circle = document.createElementNS(ns, "circle");
    for (const [k, v] of Object.entries({ cx: x(player.observed_dates[i]), cy: y(value), r: 3.5, class: "observed" })) circle.setAttribute(k, v);
    const title = document.createElementNS(ns, "title"); title.textContent = `${player.observed_dates[i]}：${number(value, 1)} 分鐘`;
    circle.append(title); svg.append(circle);
  });
  return svg;
}

export function calibrationChart(bins) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  for (const [key, value] of Object.entries({viewBox:"0 0 340 250", class:"calibration", role:"img", "aria-label":"校準圖：橫軸預測機率、縱軸實際比例；虛線是完全吻合"})) svg.setAttribute(key, value);
  const element = (name, attributes, text = "") => {const node = document.createElementNS(ns, name); for (const [k,v] of Object.entries(attributes)) node.setAttribute(k,v); node.textContent = text; svg.append(node); return node;};
  element("line", {x1:40, y1:210, x2:250, y2:0, stroke:"#889588", "stroke-dasharray":"5 5"});
  element("line", {x1:40, y1:210, x2:280, y2:210, stroke:"#667267"});
  element("line", {x1:40, y1:210, x2:40, y2:0, stroke:"#667267"});
  element("text", {x:115,y:245,fill:"currentColor"}, "預測機率 0 → 1");
  element("text", {x:45,y:15,fill:"currentColor"}, "實際比例");
  const valid = bins.filter(b => b.count && b.predicted !== null && b.observed !== null);
  element("polyline", {points: valid.map(b=>`${40+b.predicted*210},${210-b.observed*210}`).join(" "), fill:"none", stroke:"#236a50", "stroke-width":2});
  for (const b of valid) {const point = element("circle", {cx:40+b.predicted*210, cy:210-b.observed*210, r:4, fill:"#236a50"}); const title=document.createElementNS(ns,"title"); title.textContent=`${b.count} 筆：預測 ${percent(b.predicted)}、實際 ${percent(b.observed)}`; point.append(title);}
  return svg;
}
