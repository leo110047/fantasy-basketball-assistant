import {el, node, option} from "/view.js";
import {matches} from "/presentation.js";

function formValue(form) {
  return JSON.stringify([...form.elements].filter(e => e.name).map(e => [e.name,e.value,e.checked ?? null]));
}
function protect(dialog, form, close, context) {
  let initial = "", opener = null, down = false;
  const dismiss = async event => {
    event?.preventDefault();
    if (context.isSaving() || (formValue(form) !== initial && !await context.confirmChange("放棄尚未套用的修改？"))) return;
    dialog.close();
  };
  dialog.addEventListener("cancel", dismiss); close.addEventListener("click", dismiss);
  const outside = event => { const r = dialog.getBoundingClientRect(); return event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom; };
  dialog.addEventListener("pointerdown", e => { down = e.target === dialog && outside(e); });
  dialog.addEventListener("pointerup", e => { if (down && e.target === dialog && outside(e)) dismiss(e); down = false; });
  dialog.addEventListener("close", () => { if (opener?.isConnected) opener.focus(); else el(el("playerDialog").open ? "closePlayer" : "lookup").focus(); });
  return () => { form.querySelector("[role=alert]").hidden = true; initial = formValue(form); opener = document.activeElement; dialog.showModal(); };
}
async function submit(form, dialog, candidate, context) {
  const fields = form.querySelector("fieldset"); fields.disabled = true;
  const warning = form.querySelector("[role=alert]"); warning.hidden = true;
  try {
    if (await context.save(candidate)) dialog.close();
    else showError(form, context.lastError());
  }
  catch (e) { showError(form, e.message); }
  finally { fields.disabled = false; }
}
function showError(form, message) {
  const warning = form.querySelector("[role=alert]"); warning.textContent = message; warning.hidden = false;
}
export function installEditors(context) {
  const saleForm = el("editSaleForm"), overrideForm = el("overrideForm");
  const openSale = protect(el("editSaleDialog"), saleForm, el("cancelSaleEdit"), context);
  const openOverride = protect(el("overrideDialog"), overrideForm, el("cancelOverride"), context);
  let saleId = null, playerId = null;
  function fillSalePlayers() {
    const current = el("editPlayer").value;
    const items = [...context.players().values()].filter(p => p.id === current || matches(p,el("editPlayerSearch").value));
    el("editPlayer").replaceChildren(...items.map(p => option(p.id,p.name)));
    el("editPlayer").value = current;
  }
  el("editPlayerSearch").addEventListener("input", fillSalePlayers);
  saleForm.addEventListener("submit", event => {
    event.preventDefault(); if (context.isBusy()) return;
    try {
      const current = context.desk().state;
      const sales = current.sales.map(s => s.id === saleId ? {...s,player_id:el("editPlayer").value,buyer:el("editBuyer").value,amount:context.integer(el("editAmount").value)} : s);
      submit(saleForm,el("editSaleDialog"),{...current,sales},context);
    } catch (e) { showError(saleForm, e.message); }
  });
  el("overridePositionsEnabled").addEventListener("change", () => { el("overridePositions").disabled = !el("overridePositionsEnabled").checked; });
  overrideForm.addEventListener("submit", event => {
    event.preventDefault(); if (context.isBusy()) return;
    try {
      const text = el("overrideMarket").value.normalize("NFKC").trim();
      if (text && (!/^(?:\d+)(?:\.\d+)?$/.test(text) || !Number.isFinite(Number(text)))) throw new Error("市場報價請填非負數字；留白表示使用原始來源。");
      const positions = el("overridePositionsEnabled").checked ? [...el("overridePositions").querySelectorAll("input:checked")].map(e => e.value) : null;
      if (!text && positions == null) throw new Error("請指定市場價或確認位置；要還原來源請使用「移除本球員覆寫」。");
      const reason = el("overrideReason").value.trim();
      if (!reason) throw new Error("請填寫覆寫理由。");
      const entry = {player_id:playerId,market:text ? Number(text) : null,positions,reason};
      const current = context.desk().state;
      submit(overrideForm,el("overrideDialog"),{...current,overrides:[...current.overrides.filter(o => o.player_id !== playerId),entry]},context);
    } catch (e) { showError(overrideForm, e.message); }
  });
  el("removeOverride").addEventListener("click", () => {
    if (context.isBusy()) return;
    const current = context.desk().state;
    submit(overrideForm,el("overrideDialog"),{...current,overrides:current.overrides.filter(o => o.player_id !== playerId)},context);
  });
  return {
    sale(id) {
      if (context.isBusy()) return;
      const current = context.desk().state, sale = current.sales.find(s => s.id === id);
      if (!sale) return;
      saleId = id; el("editPlayerSearch").value = "";
      el("editPlayer").replaceChildren(...[...context.players().values()].map(p => option(p.id,p.name)));
      el("editPlayer").value = sale.player_id;
      el("editBuyer").replaceChildren(...current.teams.map(t => option(t.id,t.name)));
      el("editBuyer").value = sale.buyer; el("editAmount").value = sale.amount;
      el("editSaleTitle").textContent = `更正第 ${current.sales.indexOf(sale)+1} 筆成交`;
      openSale();
    },
    override(id) {
      if (context.isBusy()) return;
      playerId = id;
      const player = context.players().get(id), original = context.desk().state.overrides.find(o => o.player_id === id);
      el("overrideTitle").textContent = `${player.name} · 市場價與位置`;
      el("overrideMarket").value = original?.market ?? ""; el("overrideReason").value = original?.reason ?? "";
      el("overridePositionsEnabled").checked = original?.positions != null;
      el("overridePositions").disabled = original?.positions == null;
      el("overridePositions").replaceChildren(...context.boot().league.positions.map(position => {
        const label = node("label", position), input = document.createElement("input");
        input.type = "checkbox"; input.name = "positions"; input.value = position; input.checked = (original?.positions ?? player.positions).includes(position);
        label.prepend(input); return label;
      }));
      el("removeOverride").hidden = !original;
      openOverride();
    }
  };
}
