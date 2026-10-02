const get = id => document.getElementById(id);
let token = location.hash.slice(1);
try {
  if (token && token !== "modes") sessionStorage.setItem("workspace-session",token);
  else token = sessionStorage.getItem("workspace-session") ?? "";
  if (location.hash) history.replaceState(null,"",location.pathname + location.search);
} catch { /* The initial fragment is still usable when storage is disabled. */ }

async function request(path, body) {
  const response = await fetch(path,{method:body ? "POST" : "GET",headers:{Authorization:`Bearer ${token}`,"Content-Type":"application/json"},...(body ? {body:JSON.stringify(body)} : {})});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error ?? "無法連線，請重新啟動入口。");
  return data;
}
function message(error) {get("error").textContent=error.message;get("error").hidden=false;}
function busy(value) {
  document.querySelectorAll("button, input").forEach(node=>{node.disabled=value;});
  get("modes").setAttribute("aria-busy",String(value));
}
async function open(mode,auction) {
  busy(true);get("error").hidden=true;
  get("status").textContent=mode==="season" ? "正在開啟賽季助手…" : "正在讀取競標資料並啟動工具…";
  try {
    const result=await request("/api/open",{mode,...(auction ? {auction} : {})});
    const target=new URL(result.url);
    if (target.protocol!=="http:" || target.hostname!=="127.0.0.1" || !target.port || target.username || target.password) throw new Error("工具回傳了無效的本機網址。");
    location.assign(target.href);
  } catch (error) {message(error);get("status").textContent="";busy(false);}
}
get("openSeason").addEventListener("click",()=>open("season"));
get("auctionForm").addEventListener("submit",event=>{
  event.preventDefault();
  open("auction",Object.fromEntries(new FormData(event.currentTarget)));
});
get("auctionForm").addEventListener("invalid",()=>{get("auctionFiles").open=true;},true);
get("reload").addEventListener("click",()=>location.reload());
get("skip").addEventListener("click",event=>{event.preventDefault();get("modes").focus();});
get("quit").addEventListener("click",async()=>{
  if (!confirm("結束入口與由它啟動的工具？已另行開啟的工具會繼續執行。")) return;
  busy(true);
  try {await request("/api/quit",{});get("status").textContent="工作區正在結束。可以關閉這個分頁。";}
  catch (error) {message(error);busy(false);}
});
async function start() {
  const settings=await request("/api/bootstrap");
  if (settings.auction) {
    for (const [name,value] of Object.entries(settings.auction)) get("auctionForm").elements.namedItem(name).value=value;
    get("auctionFiles").open=false;
    get("filesSummary").textContent="更換競標資料";
    get("savedDraft").textContent=`接續名單：${settings.auction.draft_path}`;
    get("savedDraft").hidden=false;
  }
  get("status").textContent="";busy(false);
}
start().catch(error=>{message(error);get("status").textContent="請從啟動器開啟工作區。";get("reload").hidden=false;get("reload").disabled=false;});
