// 从牙e在线页面读 Cookie 里的 Authorization_web 写进 config/yae_token.json。
//
// 为什么需要这一步：牙e的鉴权值只在 Cookie 里（document.cookie 能读到，因为它没设
// HttpOnly），而请求头的键名要写成小写开头的 authToken。localStorage 里没有 token
// （只有 __user__ / cip 等）。Python 侧不可能自己去读浏览器的 Cookie 库——Edge 运行
// 期间那个 SQLite 是独占锁的，复制都复制不了。所以用 CDP 在页面里读一次。
//
// 退出码：0 成功 / 2 未登录（Cookie 里没有 Authorization_web）/ 3 CDP 连不上。
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const argv = process.argv.slice(2);
function arg(name, dflt) {
  const i = argv.indexOf("--" + name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt;
}
const PORT = process.env.MEIYA_CDP_PORT || arg("port", "9335");
const BASE = "http://127.0.0.1:" + PORT;
const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT = arg("out", path.join(HERE, "..", "config", "yae_token.json"));

async function jget(p) {
  const r = await fetch(BASE + p);
  return r.json();
}

async function main() {
  let ver;
  try {
    ver = await jget("/json/version");
  } catch (e) {
    console.log("CDP_UNREACHABLE " + BASE + " " + e.message);
    process.exit(3);
  }
  const list = await jget("/json/list");
  const pages = list.filter(t => t.type === "page" && /yae920/.test(t.url || ""));
  let t = pages.filter(p => /shoporder/i.test(p.url)).pop() || pages.pop();
  if (!t) {
    // 没有牙e标签就开一个；登录态靠浏览器自己的 Cookie，新标签同样是已登录
    await fetch(BASE + "/json/new?https://www.yae920.com/", { method: "PUT" });
    await new Promise(r => setTimeout(r, 6000));
    const l2 = await jget("/json/list");
    t = l2.filter(x => x.type === "page" && /yae920/.test(x.url || "")).pop();
  }
  if (!t) {
    console.log("NO_TARGET 没有可用的牙e页面");
    process.exit(3);
  }
  const ws = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener("open", res, { once: true });
    ws.addEventListener("error", rej, { once: true });
  });
  let id = 0;
  const pend = new Map();
  ws.addEventListener("message", ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
  });
  const send = (method, params) => new Promise(res => {
    const i = ++id; pend.set(i, res);
    ws.send(JSON.stringify({ id: i, method, params }));
  });
  const exp = "(function(){var m=document.cookie.match(/(?:^|;\\s*)Authorization_web=([^;]+)/);" +
    "var u=null;try{u=JSON.parse(localStorage.getItem('__user__')||'null');}catch(e){}" +
    "return {href:location.href,cookie:document.cookie,user:u};})()";
  const r = await send("Runtime.evaluate", { expression: exp, returnByValue: true });
  const v = (r.result && r.result.result && r.result.result.value) || {};
  const cookie = v.cookie || "";
  const m = cookie.match(/(?:^|;\s*)Authorization_web=([^;]+)/);
  if (!m) {
    console.log("NEED_LOGIN_YAE 页面 " + (v.href || "?") + " 的 Cookie 里没有 Authorization_web，请在浏览器登录牙e在线");
    ws.close();
    process.exit(2);
  }
  const tok = decodeURIComponent(m[1]);
  const u = v.user || {};
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  fs.writeFileSync(OUT, JSON.stringify({
    token: tok,
    saved_at: new Date().toISOString().slice(0, 19).replace("T", " "),
    source: "cdp-cookie:" + BASE,
    userId: u.userId || null,
    nickName: u.nickName || null,
    mobile: u.userMobile || null,
    page: v.href || null,
  }, null, 1), "utf8");
  console.log("TOKEN_SAVED_YAE len=" + tok.length + " user=" + (u.nickName || "?") + " -> " + OUT);
  ws.close();
}

main().catch(e => { console.log("ERR " + e.message); process.exit(3); });
