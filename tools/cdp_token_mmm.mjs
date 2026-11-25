// cdp_token_mmm.mjs —— 从已登录的梅苗苗页面 Cookie 里取 token（JWT）写盘。
//
// 为什么从 Cookie 取而不是 localStorage：梅苗苗把 JWT 放在 Cookie 的 token 键里
// （HttpOnly=false，document.cookie 直接可读），localStorage 里只有 homeUserPanel /
// userId 这类非鉴权信息。抓错地方会得到一个调不通接口的字符串，还要多一轮排查。
//
// 退出码：0 已保存 / 2 页面没登录 / 3 CDP 连不上
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PORT = process.env.MEIYA_CDP_PORT || "9335";
const BASE = "http://127.0.0.1:" + PORT;
const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.join(HERE, "..", "config", "mmm_token.json");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function pickTarget() {
  const tl = await (await fetch(BASE + "/json/list")).json();
  const pages = tl.filter((t) => t.type === "page");
  let p = pages.find((t) => (t.url || "").includes("mmm920"));
  if (p) return { target: p, created: false };
  // 没有梅苗苗标签就新开一个：用户此前登录过，Cookie 还在，页面会直接是登录态。
  const r = await fetch(BASE + "/json/new?https://www.mmm920.com/user/my_order", { method: "PUT" });
  if (!r.ok) throw new Error("无法新建标签 " + r.status);
  const t = await r.json();
  return { target: t, created: true };
}

(async () => {
  let target;
  try {
    const got = await pickTarget();
    target = got.target;
    if (got.created) await sleep(6000);
  } catch (e) {
    console.log("CDP_UNREACHABLE " + e.message);
    process.exit(3);
  }
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  let id = 0;
  const waiters = new Map();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiters.has(m.id)) { waiters.get(m.id)(m); waiters.delete(m.id); }
  };
  await new Promise((r) => (ws.onopen = r));
  const send = (method, params = {}) =>
    new Promise((res) => { const i = ++id; waiters.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
  const ev = async (expression) => {
    const r = await send("Runtime.evaluate", { expression, returnByValue: true });
    return r.result && r.result.result && r.result.result.value;
  };

  if (!String(await ev("location.href")).includes("mmm920")) {
    await send("Page.navigate", { url: "https://www.mmm920.com/user/my_order" });
    await sleep(6000);
  }
  const ck = (await ev("document.cookie")) || "";
  const m = ck.match(/(?:^|;\s*)token=([^;]+)/);
  if (!m) {
    console.log("NEED_LOGIN_MMM 浏览器里没有梅苗苗登录态，请在该标签页登录（https://www.mmm920.com）");
    ws.close();
    process.exit(2);
  }
  const payload = { token: m[1], saved_at: new Date().toISOString().replace("T", " ").slice(0, 19), source: "cdp-cookie:" + BASE };
  fs.writeFileSync(OUT, JSON.stringify(payload, null, 1), "utf8");
  console.log("TOKEN_SAVED_MMM len=" + m[1].length + " -> " + OUT);
  ws.close();
})().catch((e) => { console.log("ERR " + e.message); process.exit(1); });
