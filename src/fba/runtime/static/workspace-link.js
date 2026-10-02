const link = document.querySelector("#workspaceLink");
const home = new URLSearchParams(location.search).get("workspace");
if (link && home) {
  try {
    const url = new URL(home);
    if (url.protocol === "http:" && url.hostname === "127.0.0.1" && /^\d+$/.test(url.port) &&
        Number(url.port) > 0 && Number(url.port) <= 65535 && url.pathname === "/" &&
        !url.username && !url.password && !url.search && !url.hash) {
      link.href = url.href;
      link.hidden = false;
    }
  } catch { /* Standalone mode remains usable without a workspace link. */ }
}
