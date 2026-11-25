// cdp_login_state.mjs —— 只读地问一句：浏览器开着吗？两个平台各自登录着吗？
//
// 为什么单独做一个脚本，而不是让 ui.py 自己去连 CDP：
// python 这边没有 websocket 客户端，读 document.cookie 必须走 CDP 的
// Runtime.evaluate；而且「判断登录态」的正确位置就是页面里（Cookie 是页面域下的）。
//
// 三条铁律（踩过才写下来）：
// 1. 只读，绝不 PUT /json/new 开标签。以前 cdp_token_* 会在没有标签时新开一个，
//    放在「状态检查」里就等于每次刷新界面都多开一个窗口。
// 2. 不写任何文件。config/*_token.json 由 cdp_token_* 在真正拉单前写。
// 3. 任何异常都打成一行 JSON（ok:false），不要 exit 非零 —— 界面拿它只为点灯，
//    灯不亮不该让页面报错。
//
// 输出（一行 JSON）：{"cdp":true,"mmm":{"logged":true,"tab":true,"href":"..."},"yae":{...}}
import fs from "node:fs";

const argv = process.argv.slice(2);
function arg(name, dflt) {
  const i = argv.indexOf("--" + name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt;
}
const PORT = process.env.PLATFORM_CDP_PORT || arg("port", "9335");
const BASE = "http://127.0.0.1:" + PORT;

const PLATFORMS = [
  { key: "mmm", name: "梅苗苗", host: /mmm920/, login: "https://www.mmm920.com/login",
    pick: (u) => /mmm920/.test(u) },
  { key: "yae", name: "牙e在线", host: /yae920/, login: "https://www.yae920.com/",
    pick: (u) => /yae920/.test(u) && /shoporder/i.test(u) || /yae920/.test(u) },
];

async function cookieOf(wsUrl, extra) {
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => {
    ws.addEventListener("open", res, { once: true });
    ws.addEventListener("error", rej, { once: true });
  });
  let id = 0;
  const pend = new Map();
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
  });
  const send = (method, params) => new Promise((res) => {
    const i = ++id; pend.set(i, res);
    ws.send(JSON.stringify({ id: i, method, params }));
  });
  const expr = "(function(){var o={href:location.href,cookie:document.cookie};" +
    (extra ? extra : "") + "return o;})()";
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true });
  ws.close();
  const v = (r.result && r.result.result && r.result.result.value) || {};
  return v;
}

function loggedIn(key, cookie) {
  const ck = String(cookie || "");
  if (key === "mmm") return /(?:^|;\s*)token=[^;]+/.test(ck);
  return /(?:^|;\s*)Authorization_web=[^;]+/.test(ck);
}

(async () => {
  const out = { cdp: false, at: new Date().toISOString().slice(0, 19).replace("T", " ") };
  let list;
  try {
    const r = await fetch(BASE + "/json/list");
    if (!r.ok) throw new Error("HTTP " + r.status);
    list = await r.json();
  } catch (e) {
    out.error = "CDP_UNREACHABLE " + e.message;
    for (const p of PLATFORMS) out[p.key] = { logged: false, tab: false, why: "浏览器没带调试端口启动" };
    console.log(JSON.stringify(out));
    return;
  }
  out.cdp = true;
  const pages = (list || []).filter((t) => t.type === "page");
  for (const p of PLATFORMS) {
    const cands = pages.filter((t) => p.pick(t.url || ""));
    const t = cands.pop();
    if (!t) { out[p.key] = { logged: false, tab: false, why: "没有打开 " + p.name }; continue; }
    try {
      const v = await cookieOf(t.webSocketDebuggerUrl,
        p.key === "yae" ? "try{o.user=JSON.parse(localStorage.getItem('__user__')||'null');}catch(e){}" : "");
      out[p.key] = { logged: loggedIn(p.key, v.cookie), tab: true, href: v.href || t.url,
                     user: (v.user && (v.user.nickName || v.user.mobile)) || null,
                     why: loggedIn(p.key, v.cookie) ? "" : "页面在 " + (v.href || t.url) + "，Cookie 里没有登录凭证" };
    } catch (e) {
      out[p.key] = { logged: false, tab: true, href: t.url, why: "读 Cookie 失败：" + e.message };
    }
  }
  console.log(JSON.stringify(out));
})();
