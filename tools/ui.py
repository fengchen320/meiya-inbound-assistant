# -*- coding: utf-8 -*-
"""ui.py —— 本地交互界面（零第三方依赖）。

为什么要有界面：
    这条链现在有 4 个入口（平台拉单 / 本地表格 / 照片 / 直接给目录）、9 种交付状态、
    每份单据几十条告警。用命令行跑没问题，但「这一单卡在哪、要不要我管」这件事
    只能靠人读日志判断。界面不是把 CLI 换个皮，而是把 pipeline_summary.json 里
    本来就有的结构化事实摊开：状态色块、供货单位、明细行数、五列表头校验。

设计约束（都是刻意的）：
1. 零第三方依赖。只用 http.server / json / subprocess / threading / PIL（仅缩略图，
   没有就退化成原图）。和 yamlite.py 自己解 YAML 是同一个取舍：这是装在别人机器上
   的工具，多一个依赖就多一个装不上的理由。
2. 服务只绑 127.0.0.1。它能读本机文件、能驱动已登录的美亚会话——绑 0.0.0.0 等于
   把「以你的身份往库存里写东西」这件事开放给同网段。
3. 提交（--commit）默认关闭，且要人手动勾 + 输入单号确认。库存加错了要盘点到才发现。
4. 进度靠 SSE 逐行推；pipeline 用 --evt 输出 EVT {json}，普通行原样透传。
   解析人话日志当进度是不可维护的（改一个字前端就瞎）。

用法：
    python tools/ui.py            # 打开 http://127.0.0.1:8799
    python tools/ui.py --open     # 顺手拉起默认浏览器
    python tools/ui.py --port 9000
"""
import argparse
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
sys.path.insert(0, TOOLS)

# 禁令先装钩子（no_meiya 在 import 时包住 socket/urllib 出口），必须排在其它本地模块之前。
# 界面进程也会走拉单（lib_yae/lib_mmm），在这里装一次就够，子进程各自是独立进程、
# 由 pipeline.py / run.py 的入口再装一次。
import no_meiya  # noqa: E402,F401  禁止访问美亚美牙（myyun.com）
import expiry as expiry_mod      # noqa: E402  效期审批（界面「同意入库」按钮）
import ledger            # noqa: E402
import localdb           # noqa: E402  本地产品库（data/local.db）
import manual_fixes as manual_fixes_mod  # noqa: E402  人工处置（分类/拆包/这批不入）
import reject as reject_mod     # noqa: E402  禁止入库（赠品/样品/自备料）

UPLOAD_DIR = os.path.join(ROOT, "tmp", "ui_upload")
RUN_DIR = os.path.join(ROOT, "tmp", "ui_run")
OUT_ROOT = os.path.join(ROOT, "out", "ui")
PROFILE_DIR = os.path.join(ROOT, "config", "photo_profiles")
PAGE_FILE = os.path.join(TOOLS, "ui_page.html")

IMG_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
SHEET_EXT = (".xls", ".xlsx", ".xlsm")
SUPPORTED = IMG_EXT + SHEET_EXT

STATE_GROUP = {
    # 新定位（2026-11-23 起，不再连美亚）：
    #   emitted  已出表 —— 这是终点，不是中间态
    #   need_build 本地库里没有这些物品 → 先导 products.xlsx 建档，再出一遍表
    #   empty    没有可入的行（全被赠品/禁入/效期筛掉）
    "emitted": "ok",
    "need_build": "warn",
    "empty": "warn",
    "committed": "ok",
    "ready": "ok",
    "pending_build": "warn",
    "blocked_build": "bad",
    # 档案歧义：本地库同一身份有多条档案，人得选一条。它不是「失败」也不是
    # 「去建档」，但也不能算正常 —— 不选这条货就入不了，所以用红。
    "ambiguous": "bad",
    "no_products_file": "bad",
    "upload_failed": "bad",
    "rebuild_failed": "bad",
    "inbound_failed": "bad",
    "supplier_unmatched": "bad",
    # 判重不是失败：duplicate 是我们想要的正常结果，用 warn 色而不是红色，
    # 否则人一看到红就以为出错了，跑去 --force 再入一次。
    "duplicate": "warn",
    # 整单都是赠品不是失败：与 duplicate 同理，用黄不用红，否则人会去补数据。
    "skipped_gift": "warn",
    # 效期不足不是「程序出错」也不是「数据要补」，是等人拍板：用红（安全项），
    # 但页面会给一个「同意入库」按钮，避免人误以为要改单据。
    "awaiting_approval": "bad",
    # 人工判定「禁止入库」不是失败，是已经处理完了：用灰不用户外红，
    # 否则每次打开页面都像有一堆错误没解决。
    "all_rejected": "warn",
    "ledger_conflict": "bad",
    "docno_missing": "bad",
}


def python_bin():
    for c in (os.environ.get("DSH_PYTHON"),
              sys.executable):
        if c and os.path.exists(c):
            return c
    return sys.executable


def node_bin():
    for c in (os.environ.get("DSH_NODE"),
              "node"):
        if c == "node" or (c and os.path.exists(c)):
            return c
    return "node"


# 本机上能找到的 Edge（唯一被支持的浏览器）
EDGE_CANDIDATES = (r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
                   r"C:/Program Files/Microsoft/Edge/Application/msedge.exe")


def edge_bin():
    """返回 Edge 可执行文件路径；找不到返回 None。

    为什么全程序只认 Edge、不认系统默认浏览器：程序的落单链路（--user-data-dir
    那个 profile 里的 Cookie、CDP 调试端口）都在 Edge 里。用户机器上系统默认
    浏览器是 Chrome（实测 UserChoice http/https = ChromeHTML），webbrowser.open()
    和 bat 里的 start "" http://… 都会开 Chrome —— 那是一个和程序毫无关系的窗口，
    人在里面登录、程序照样说「没登录」。所以凡是「替用户打开一个网页」的地方，
    都必须显式点名 Edge，不许走系统默认。
    """
    for c in (os.environ.get("PLATFORM_EDGE"),) + EDGE_CANDIDATES:
        if c and os.path.exists(c):
            return c
    return None


# 调试端口与调试专用 profile：拉单全靠它们，程序自己开浏览器时必须带上。
CDP_PORT_DEFAULT = "9335"
# 配置目录可用环境变量覆盖：临时起第二个实例自测时，绝不能让两个进程抢同一个
# profile —— 后启动的进程只会把参数转给先启动的，调试端口永远不开（见下面的诊断）。
DEBUG_PROFILE = os.environ.get("PLATFORM_DEBUG_PROFILE") or os.path.join(ROOT, "tmp", "prof_login")


def cdp_port():
    return os.environ.get("PLATFORM_CDP_PORT", CDP_PORT_DEFAULT)


def cdp_version(port=None):
    """调试端口上跑的是什么浏览器；没起来返回 None。

    这是「浏览器带没带调试端口」唯一可靠的判据：普通方式打开的 Edge 也监听
    很多端口，但只有带 --remote-debugging-port 的才响应 /json/version。
    """
    import urllib.request
    port = port or cdp_port()
    try:
        with urllib.request.urlopen("http://127.0.0.1:%s/json/version" % port,
                                    timeout=2) as r:
            return (json.loads(r.read().decode("utf-8")) or {}).get("Browser")
    except Exception:
        return None


def ensure_debug_browser(wait=25.0):
    """确保「带调试端口的 Edge」在跑，没有就自己拉起来。返回 (ok, 说明)。

    为什么必须由程序来开：用户双击桌面上的 Edge 打开的是**日常 profile、无调试端口**，
    程序读不到 Cookie，界面上却只会说「登录态不可用」——人会去重新登录，登录完还是
    不行。启动参数是固定死的，没有理由指望人手动加。
    """
    v = cdp_version()
    if v:
        return True, "已经在跑（%s）" % v
    exe = edge_bin()
    if not exe:
        return False, "找不到 Edge 可执行文件"
    os.makedirs(DEBUG_PROFILE, exist_ok=True)
    try:
        subprocess.Popen([exe, "--remote-debugging-port=%s" % cdp_port(),
                          "--user-data-dir=%s" % DEBUG_PROFILE, "--start-maximized"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return False, "启动浏览器失败：%s" % e
    # 冷启动到调试端口可用一般 3–8 秒；不写死 sleep 是因为机器慢时固定值会让人
    # 以为失败又点一次，结果开出第二个窗口。
    t0 = time.time()
    while time.time() - t0 < wait:
        time.sleep(0.5)
        if cdp_version():
            return True, "刚启动"
    # 一个真实的坑：如果已经有一个 Edge 用着同一个 --user-data-dir 但没开调试端口，
    # 新进程只会把参数转给老进程、调试端口永远不开。这种情况直接说出来，
    # 否则用户只会看到「等了 25 秒没响应」，然后反复点、反复没反应。
    if _edge_using_profile(DEBUG_PROFILE):
        return False, ("配置目录 %s 已被另一个 Edge 实例占用（它可能带的是别的调试端口）。"
                       "先把它全部关掉再点「打开浏览器」" % DEBUG_PROFILE)
    return False, "浏览器起来了但调试端口 %s 在 %d 秒内没响应" % (cdp_port(), int(wait))


def _edge_using_profile(prof):
    """有没有 msedge 进程正用着这个配置目录（用来诊断「端口打不开」）。"""
    try:
        ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        r = subprocess.run([ps, "-NoProfile", "-Command",
                            "(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
                            "Where-Object { $_.CommandLine -like '*%s*' }).Count" % prof],
                           capture_output=True, text=True, timeout=20)
        return (r.stdout or "").strip().isdigit() and int((r.stdout or "0").strip()) > 0
    except Exception:
        return False


def open_page_in_debug_browser(url):
    """把页面开在**带调试端口的那个 Edge** 里；已经开着就只切过去。

    这是「双击助手只出现一个窗口」的关键：以前 --open 走 open_page_in_edge()，
    开的是普通 Edge 窗口（没有调试端口），于是界面上永远显示「浏览器没带调试端口」，
    用户还得再点一次「打开浏览器」，又冒出一个窗口——两个窗口一个连着一个不连，
    人分不清该在哪个里登录。
    """
    import urllib.request
    ok, msg = ensure_debug_browser()
    print("  调试浏览器：%s" % msg)
    if not ok:
        print("  退回普通窗口打开 %s（拉单会不可用，可再点界面上的「打开浏览器」）" % url)
        open_page_in_edge(url)
        return ""
    port = cdp_port()
    base = "http://127.0.0.1:%s" % port
    try:
        with urllib.request.urlopen(base + "/json/list", timeout=5) as r:
            tabs = json.loads(r.read().decode("utf-8")) or []
    except Exception:
        tabs = []
    # 已经开着这个页面就别叠标签：反复双击助手会积一堆同名标签
    for t in tabs:
        if t.get("type") == "page" and str(t.get("url") or "").startswith(url.rstrip("/")):
            try:
                req = urllib.request.Request(base + "/json/activate/" + t.get("id", ""),
                                             method="PUT")
                urllib.request.urlopen(req, timeout=5).read()
            except Exception:
                pass
            return t.get("id") or ""
    try:
        req = urllib.request.Request(base + "/json/new?" + urllib.parse.quote(url, safe=""),
                                     method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            t = json.loads(r.read().decode("utf-8")) or {}
        return t.get("id") or ""
    except Exception as e:
        print("  在调试浏览器里开标签失败（%s），退回普通窗口" % e)
        open_page_in_edge(url)
        return ""


def open_page_in_edge(url):
    """把页面开在本机 Edge 里；找不到 Edge 才退回系统默认浏览器。

    单独拎成函数是为了可测：这段以前直接写在 main() 的 lambda 里，唯一能验的
    办法是真去点一次开始菜单，而「开成了 Chrome」这种事肉眼也未必立刻反应过来
    （两个浏览器窗口长得一样，只有任务栏图标不同）。
    """
    exe = edge_bin()
    try:
        if exe:
            subprocess.Popen([exe, url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return exe
        print("  没找到 Edge，请手动打开 %s" % url)
        webbrowser.open(url)      # 兜底：总有页面比没有强
        return ""
    except Exception as e:
        print("  打开浏览器失败（%s），请手动打开 %s" % (e, url))
        return ""


def profiles():
    """可选的照片版式。界面上的下拉就用这个，不写死——新增 profile 文件即出现。"""
    out = []
    try:
        for fn in sorted(os.listdir(PROFILE_DIR)):
            if fn.endswith(".json"):
                pid = fn[:-5]
                label = pid
                try:
                    with open(os.path.join(PROFILE_DIR, fn), encoding="utf-8") as f:
                        j = json.load(f)
                    label = j.get("doc_type") or pid
                except Exception:
                    pass
                out.append({"id": pid, "label": label})
    except OSError:
        pass
    return out


# ── 子进程与进度广播 ────────────────────────────────────────────────
class Hub:
    """一条命令的进度中枢：pipeline 的 stdout 逐行进来，SSE 订阅者各自取走。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.subs = []
        self.backlog = []
        self.proc = None
        self.state = "idle"     # idle / running / done / stopped / failed
        self.started = None
        self.rc = None
        # 这一轮写到哪个目录。必须记在 Hub 上：界面刷新/新开一个标签页后
        # 前端只有 SSE 的 hello，拿不到「刚跑完的是哪一批」，结果表会一直空着
        # （实测：跑完三单 ready，刷新页面后右下的「单据结果」显示「还没有跑过」）。
        self.out = None

    def reset(self):
        with self.lock:
            self.backlog = []
            self.state = "running"
            self.started = time.time()
            self.rc = None

    def publish(self, kind, **kw):
        rec = {"kind": kind}
        rec.update(kw)
        with self.lock:
            self.backlog.append(rec)
            # 全量 backlog 会被前端整段重放，长时间跑会吃内存，留最近 4000 行足够
            # 定位问题（真要看全的就读 out/ui/<批次>/run.log）。
            if len(self.backlog) > 4000:
                self.backlog = self.backlog[-3000:]
            subs = list(self.subs)
        for q in subs:
            try:
                q.append(rec)
            except Exception:
                pass

    def subscribe(self):
        q = []
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)


HUB = Hub()


def _emit_line(line):
    line = line.rstrip("\r\n")
    if line.startswith("EVT "):
        try:
            HUB.publish("evt", evt=json.loads(line[4:]))
            return
        except ValueError:
            pass
    if line.strip():
        HUB.publish("line", text=line)


# ── 子进程管理 ─────────────────────────────────────────────────────
def _kill_tree(proc):
    """杀进程树。只 terminate 父进程会留下 node 子进程（CDP 脚本），
    下次再跑就会争同一个浏览器端口。"""
    if not proc or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
        else:
            proc.terminate()
    except Exception:
        pass


def _resume_cmd(cmd, src):
    """把「重跑这一批」的命令改成读已落盘的数据，而不是重新去平台拉一遍。

    为什么要改：处置（效期审批 / 补分类）只在出表这一步生效，订单数据在第一次
    跑的时候就已经落在磁盘上了。照原样重放 **--pull** 有两个坏处：
      1. 慢且要登录态，登录过期时重跑直接失败，人以为处置没生效；
      2. 平台上的订单可能已经变了，重跑拿到的行跟上一轮审批的单号/有效期对不上，
         表现为「审了还是不认」——实测用户 2026-11-24 报的无限循环。
    所以：批次记了 src（pipeline 写进 _cmd.json）就用 --in <src>；
    没记（老批次）就把 --pull/--pull-from/--pull-to 摘掉，退回默认的 out/pull/<平台>。
    """
    out, i, pull, plat = [], 0, False, None
    drop_with_value = {"--pull-from", "--pull-to", "--pull-status"}
    while i < len(cmd):
        a = cmd[i]
        if a == "--pull":
            pull = True
            if i + 1 < len(cmd):
                plat = cmd[i + 1]
            i += 2
            continue
        if a in drop_with_value:
            i += 2
            continue
        out.append(a)
        i += 1
    if not pull:
        return cmd
    if src and os.path.isdir(src):
        return out + ["--in", os.path.abspath(src)]
    # 没有记下 src：用平台拉单的默认落盘目录（pipeline 里 pull_platform 的 out）。
    if plat:
        guess = os.path.join(ROOT, "out", "pull", plat)
        if os.path.isdir(guess):
            return out + ["--in", guess]
    return out


def _run_pipeline(cmd, log_path, log_mode="w"):
    """跑一轮 pipeline 并把输出逐行推给界面。

    log_mode="a" 用于「处置完接着跑」：同一个批次目录会临好几轮，
    覆盖写会把上一轮的错误现场抹掉 —— 而用户回头看的时候，最想知道的
    恰恰是「上一轮它为什么被拦下」。
    """
    HUB.reset()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    f = open(log_path, log_mode, encoding="utf-8")
    try:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", bufsize=1,
                                env=env, creationflags=flags)
        HUB.proc = proc
        HUB.publish("start", cmd=" ".join(cmd), log=log_path)
        # 静默心跳：照片走 PaddleOCR，单张要 60–75 秒且中间一行输出都没有，
        # 入库回填点弹窗也能静 20 秒。界面这时只有一片不动的旧日志，人第一反应
        # 就是「卡住了」然后点停止（实测被当成 bug 报过）。这里只是「证明还活着」，
        # 不伪造任何进度信息 —— 真进度仍来自子进程自己打印的行。
        last = [time.time()]

        def _hb():
            while HUB.proc is proc:
                time.sleep(3)
                if time.time() - last[0] >= 20:
                    HUB.publish("line", text="…… 已静默 %d 秒，子进程仍在跑（OCR 单张约 1 分钟）"
                                % (time.time() - last[0]))
                    last[0] = time.time()
        threading.Thread(target=_hb, daemon=True).start()
        for line in proc.stdout:
            last[0] = time.time()
            _emit_line(line)
            f.write(line)
            f.flush()
        proc.wait()
        HUB.rc = proc.returncode
        HUB.state = "done" if proc.returncode == 0 else "failed"
        # end 必须带上 out：前端「跑完 → 重新读结果表」靠它。只靠前端自己记 lastOut
        # 不够 —— 刷新过页面、或 /api/run 的响应还没回来就收到了 end（重启 ui.py 后
        # SSE 会重放 backlog，这时前端根本没发过 run 请求）时 lastOut 是空的，
        # 结果表就永远停在「还没有跑过」（实测踩过）。
        HUB.publish("end", rc=proc.returncode, state=HUB.state, out=HUB.out,
                    elapsed=round(time.time() - (HUB.started or time.time()), 1))
    except Exception as e:
        HUB.state = "failed"
        HUB.publish("end", rc=-1, state="failed", out=HUB.out, error=str(e))
        f.write("\n[ui] %s\n" % e)
    finally:
        try:
            f.close()
        except Exception:
            pass
        HUB.proc = None


# ── 结果汇总（把 tail 里的 REPORT 解析出来）────────────────────────────
_REPORT = re.compile(r"^REPORT=(\{.*\})\s*$", re.M)


def load_summary(out_dir):
    """读 pipeline_summary.json 并补两样界面必需、但代码里不该塞的派生数据：
    每单的 REPORT（现在埋在 inbound.tail 这一整行长字符串里）与颜色分组。"""
    p = os.path.join(out_dir, "pipeline_summary.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        j = json.load(f)
    for d in j.get("documents") or []:
        tail = ((d.get("inbound") or {}).get("tail") or "")
        rep = None
        m = _REPORT.search(tail)
        if m:
            try:
                rep = json.loads(m.group(1))
            except ValueError:
                rep = None
        d["report"] = rep
        d["group"] = STATE_GROUP.get(d.get("state"), "warn")
        # cdp_inbound 的原始终态行（老批次才有），是判断「到底填没填进去」的唯一证据
        d["clear_draft"] = next((ln for ln in tail.splitlines()
                                 if ln.startswith("CLEARED_DRAFT")), None)
        # 2026-11-23 起不再点「直接入库」：出表就是终点。
        # 老批次（有 inbound.tail 的）仍按 COMMIT=clicked 判，否则翻旧批次会全变成未提交。
        d["committed"] = (d.get("state") == "emitted") or ("COMMIT=clicked" in tail)
        # 来源（牙e在线 / 梅苗苗 / 照片 / 本地文件）。
        # 新批次由 run.py 写进 rec["origin"]；老批次没有这个字段，
        # 这里按抽取结果里的 source_sheet(platform:yae / photo:xxx) 补一次，
        # 否则翻去年的批次全是「—」，看不出这单是怎么来的。
        if not d.get("origin"):
            d["origin"] = _origin_of(d)
    return j


_ORIGIN_NAMES = {"yae": "牙e在线", "mmm": "梅苗苗", "photo": "照片"}


def _origin_of(d):
    ex = d.get("extracted") or ""
    tpl = ""
    if ex and os.path.exists(ex):
        try:
            with open(ex, encoding="utf-8") as f:
                doc = (json.load(f).get("document") or {})
            raw = str(doc.get("source_sheet_origin") or doc.get("source_sheet") or "")
            tpl = raw
        except (ValueError, OSError):
            tpl = ""
    if tpl.startswith("platform:"):
        key = tpl.split(":", 1)[1]
        return _ORIGIN_NAMES.get(key, key)
    if tpl.startswith("photo:"):
        return _ORIGIN_NAMES["photo"]
    low = str(d.get("source") or "").replace("\\", "/").lower()
    for k, v in _ORIGIN_NAMES.items():
        if "/pull/%s/" % k in low:
            return v
    return "本地文件"


def load_batch(out_dir):
    p = os.path.join(out_dir, "batch_summary.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# ── multipart（图片上传）────────────────────────────────────────────
def parse_multipart(body, boundary):
    """极简 multipart 解析：只取 filename + 内容，够用即可。

    为什么不用 cgi.FieldStorage：3.13 已移除，而且它会落临时文件、拿不到原始字节。
    """
    out = []
    sep = b"--" + boundary
    for part in body.split(sep):
        if not part or part in (b"--", b"--\r\n", b"\r\n"):
            continue
        part = part.lstrip(b"\r\n")
        if part.startswith(b"--"):
            continue
        head, _, data = part.partition(b"\r\n\r\n")
        if not _:
            continue
        data = data[:-2] if data.endswith(b"\r\n") else data
        name = re.search(rb'name="([^"]*)"', head)
        fn = re.search(rb'filename="([^"]*)"', head)
        out.append({
            "name": (name.group(1).decode("utf-8", "replace") if name else ""),
            "filename": (fn.group(1).decode("utf-8", "replace") if fn else ""),
            "data": data,
        })
    return out


def _ensure_dirs():
    for d in (UPLOAD_DIR, RUN_DIR, OUT_ROOT):
        os.makedirs(d, exist_ok=True)
    # 本地产品库（data/local.db）要在这里建出来。以前只在自己的开发机上跑，
    # 那份库早就存在，所以这个洞一直没暴露；新装用户第一次打开界面，顶栏会
    # 直接显示 "no such table: products"，看起来像装坏了。
    # init() 是幂等的（SCHEMA 全是 CREATE TABLE IF NOT EXISTS），老库不动。
    try:
        localdb.init()
    except Exception as e:
        print("[本地库] 初始化失败：%s: %s" % (type(e).__name__, e))


def _latest_out():
    """最近一个含 pipeline_summary.json 的批次目录（没有就返回 OUT_ROOT/last）。

    结果表的意义是「上一轮到底成了没成」，它不该依赖前端记不记得上一轮是哪个目录。
    **两处都扫**：界面自己跑的批次落在 out/ui/<时间戳>/，命令行跑的落在 out/<名字>/。
    只扫 out/ui 时，刚用命令行验证完（比如 --commit 那一单）打开界面会看不到，
    人第一反应是「结果没生成」而不是「界面只认自己跑的」。
    """
    best, best_t = None, -1.0
    roots = [OUT_ROOT, os.path.join(ROOT, "out")]
    seen = set()
    # 只扫一层是不够的：命令行的出表默认落在 out/emit/<批次>/，那是第二层。
    # 实测后果——用户在界面点「开始」跑完，第二天用命令行出了一批新表，
    # 界面默认显示的仍是旧的那批（全被判重跳过、每单都没有表链接），
    # 于是报「没有入库表可供下载」。所以对 out/ 下的子目录再看一层。
    for root in roots:
        try:
            names = os.listdir(root)
        except Exception:
            continue
        for nm in names:
            d = os.path.join(root, nm)
            if d in seen or not os.path.isdir(d):
                continue
            seen.add(d)
            cands = [d]
            if os.path.abspath(root) == os.path.abspath(os.path.join(ROOT, "out")):
                try:
                    cands += [os.path.join(d, sub) for sub in os.listdir(d)]
                except Exception:
                    pass
            for cand in cands:
                f = os.path.join(cand, "pipeline_summary.json")
                if not os.path.isfile(f):
                    continue
                try:
                    t = os.path.getmtime(f)
                except OSError:
                    continue
                if t > best_t:
                    best, best_t = cand, t
    return best or os.path.join(OUT_ROOT, "last")


def _safe_under(path, bases):
    """只允许访问给定目录下的文件。这个服务能读本机文件，路径必须收口。"""
    ap = os.path.abspath(path)
    for b in bases:
        try:
            if os.path.commonpath([ap, os.path.abspath(b)]) == os.path.abspath(b):
                return ap
        except ValueError:
            continue
    return None


# ── HTTP ───────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "meiya-inbound-assistant/1.0"

    def log_message(self, fmt, *args):
        pass  # 访问日志会把 SSE 的每一帧都刷屏，没人看

    # ---- 小工具 ----
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    # ---- 路由 ----
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._page()
            if u.path == "/api/status":
                return self._status()
            if u.path == "/api/login":
                return self._login()
            if u.path == "/api/summary":
                return self._summary(q)
            if u.path == "/api/list":
                return self._list(q)
            if u.path == "/api/recon":
                return self._recon(q)
            if u.path == "/api/stream":
                return self._stream()
            if u.path == "/thumb":
                return self._thumb(q)
            if u.path in ("/download", "/api/download"):
                # /api/download 是历史别名：页面一直写的是它，而服务端只认 /download，
                # 结果每个下载链接都 404——用户看到的就是「没有入库表可供下载」。
                # 两条路径都留着，免得浏览器缓存里的旧页面又变成死链。
                return self._download(q)
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            try:
                return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
            except Exception:
                pass

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/api/upload":
                return self._upload()
            if u.path == "/api/probe":
                return self._probe()
            if u.path == "/api/library":
                return self._import_library()
            if u.path == "/api/browser":
                return self._browser()
            if u.path == "/api/open":
                return self._open_tab()
            if u.path == "/api/run":
                return self._run()
            if u.path == "/api/stop":
                return self._stop()
            if u.path == "/api/fixes":
                return self._resolve()
            if u.path == "/api/approve":
                return self._approve()
            if u.path == "/api/reject":
                return self._reject()
            if u.path == "/api/confirm":
                return self._confirm()
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            try:
                return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
            except Exception:
                pass

    # ---- 各接口 ----
    def _page(self):
        with open(PAGE_FILE, "rb") as f:
            body = f.read()
        self._send(200, body, "text/html; charset=utf-8")

    def _status(self):
        """顶栏的灯：CDP（只用于牙e/梅苗苗拉单）在不在、本地产品库多少条。

        2026-11-23 起不再读美亚 token / 快照 —— 顶栏还在显示「美亚登录态」会
        让人以为程序还在连美亚（它已经不连了）。
        """
        st = {"cdp": False, "cdp_port": os.environ.get("PLATFORM_CDP_PORT", "9335"),
              "library": None, "db": None,
              "profiles": profiles(), "python": python_bin(), "node": node_bin(),
              "states": list(STATE_GROUP)}
        # 本地产品库（data/local.db）：这是现在唯一的「已建档」依据，
        # 顶栏显示它比显示美亚快照有意义——出表准不准全看它。
        try:
            s = localdb.stats()
            dbp = localdb.DB_PATH
            st["db"] = {"path": dbp, "products": s.get("products"),
                        "built": s.get("built"), "pending": s.get("pending"),
                        "idents": s.get("idents"), "docs": s.get("docs"),
                        "mtime": (time.strftime("%Y-%m-%d %H:%M",
                                                time.localtime(os.path.getmtime(dbp)))
                                  if os.path.exists(dbp) else None)}
        except Exception as e:
            st["db"] = {"error": "%s: %s" % (type(e).__name__, e)}
        try:
            import urllib.request
            port = st["cdp_port"]
            with urllib.request.urlopen("http://127.0.0.1:%s/json/version" % port, timeout=2) as r:
                j = json.loads(r.read().decode("utf-8"))
            st["cdp"] = True
            st["browser"] = j.get("Browser")
        except Exception:
            pass
        return self._json(st)

    def _browser(self):
        """把带调试端口的浏览器拉起来（已经开着就什么都不做）。

        为什么值得做成按钮：拉单链路的第一个前提是「浏览器带 --remote-debugging-port
        开着」，而这一步最容易漏 —— 用户从桌面图标正常打开的 Edge 没有调试端口，
        程序读不到 Cookie，报的是「登录态不可用」，人会去重新登录，登录完还是不行。
        实测这个坑耗掉一整轮排查。既然启动参数是固定死的，就让程序自己开。
        用独立 profile 目录：调试端口与日常浏览的窗口不能共用一份 profile。
        """
        already = bool(self._cdp_version(cdp_port()))
        ok, msg = ensure_debug_browser()
        if not ok:
            return self._json({"ok": False, "error": msg}, 500)
        return self._json({"ok": True, "already": already, "browser": self._cdp_version(),
                           "msg": msg, "profile": DEBUG_PROFILE})

    # 两个平台各自的登录页（点「去登录」时在**调试浏览器**里开，不是系统默认浏览器）
    LOGIN_URLS = {"mmm": "https://www.mmm920.com/user/my_order",
                  "yae": "https://www.yae920.com/"}

    def _open_tab(self):
        """在带调试端口的那个浏览器里开一个平台标签。

        为什么不用 <a target=_blank>：那会开在**系统默认浏览器**里，而程序读的是
        --user-data-dir 指向的那个 profile 的 Cookie。在别的窗口登录，程序照样
        说「没登录」—— 这个坑比不提供链接更坏，因为人以为已经登上了。
        所以统一走 CDP 的 PUT /json/new。
        """
        j = json.loads(self._read_body() or b"{}")
        plat = j.get("platform")
        if plat not in self.LOGIN_URLS:
            return self._json({"error": "platform 必须是 mmm 或 yae"}, 400)
        port = os.environ.get("PLATFORM_CDP_PORT", "9335")
        if not self._cdp_version(port):
            return self._json({"error": "浏览器还没带调试端口启动，先点「打开浏览器」"}, 400)
        url = self.LOGIN_URLS[plat]
        try:
            import urllib.request
            req = urllib.request.Request(
                "http://127.0.0.1:%s/json/new?%s" % (port, urllib.parse.quote(url, safe="")),
                method="PUT")
            with urllib.request.urlopen(req, timeout=10) as r:
                t = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            return self._json({"error": "开标签失败：%s" % e}, 500)
        return self._json({"ok": True, "url": t.get("url") or url, "id": t.get("id")})

    @staticmethod
    def _cdp_version(port):
        import urllib.request
        try:
            with urllib.request.urlopen("http://127.0.0.1:%s/json/version" % port,
                                        timeout=2) as r:
                return (json.loads(r.read().decode("utf-8")) or {}).get("Browser")
        except Exception:
            return None

    def _login(self):
        """问一句：浏览器开着吗？两个平台各自登录着吗？（只读，不写盘、不开标签）

        为什么要有这个接口：拉单失败的根因九成是登录态过期，而 pipeline 要跑到
        pull_platform 才发现 —— 那时子进程已经起了、日志已经刷了一屏，人看到的
        只是「点开始没反应」（实测用户就是这么报的）。把状态提前摆在顶栏，
        他点之前就知道要不要先去登录。
        """
        import subprocess
        script = os.path.join(TOOLS, "cdp_login_state.mjs")
        st = {"cdp": False, "error": None}
        try:
            r = subprocess.run([node_bin(), script, "--port",
                                os.environ.get("PLATFORM_CDP_PORT", "9335")],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=30, cwd=ROOT)
            line = ""
            for ln in (r.stdout or "").splitlines():
                if ln.strip().startswith("{"):
                    line = ln.strip()
            if line:
                st = json.loads(line)
            else:
                st = {"cdp": False,
                      "error": ((r.stdout or "") + (r.stderr or ""))[-300:].strip() or "没有输出"}
        except Exception as e:
            st = {"cdp": False, "error": "%s: %s" % (type(e).__name__, e)}
        return self._json(st)

    def _summary(self, q):
        # 不传 out（或传空）时给「最近一个跑过的批次」，而不是固定的 last 目录：
        # 界面刷新、重开标签、甚至 ui.py 重启之后，右下角的单据结果都还能看到上一轮，
        # 否则会一直显示「还没有跑过」，让人以为任务没生效。
        out_dir = (q.get("out") or [""])[0] or _latest_out()
        ap = _safe_under(out_dir, [OUT_ROOT, os.path.join(ROOT, "out")])
        if not ap:
            return self._json({"error": "out 目录不在允许范围内"}, 403)
        pipe = load_summary(ap)
        # 把「这单确认入库了没有」贴到每份单据上（用户 2026-11-25）：
        # 出表和入库是两件事，人导完美亚要回来点一下确认。界面下方那排按钮
        # 全靠这个标记显示「已入库 / 未确认」，没有它就只能显示一排一模一样的
        # 「确认入库」，人点完看不出有没有生效，自然会连点几遍。
        # 台账读失败不能拖垮结果表：确认标记只是附加信息，单据结果才是主体。
        try:
            idx = {}
            for rec in (ledger.load().get("records") or []):
                idx[ledger.norm_doc_no(rec.get("doc_no"))] = rec.get("confirmed_at") or ""
            for d in (pipe.get("documents") or []):
                k = ledger.norm_doc_no(d.get("doc_no") or d.get("stem") or "")
                at = idx.get(k) or ""
                d["confirmed_at"] = at
                d["confirmed"] = bool(at)
        except (ValueError, OSError):
            pass
        return self._json({"pipeline": pipe, "batch": load_batch(ap), "out": ap})

    def _list(self, q):
        d = (q.get("dir") or [""])[0].strip().strip('"')
        if not d:
            return self._json({"error": "缺少 dir"}, 400)
        if not os.path.isdir(d):
            return self._json({"error": "不是目录：%s" % d}, 400)
        items = []
        for fn in sorted(os.listdir(d)):
            if fn.startswith("~$") or not fn.lower().endswith(SUPPORTED):
                continue
            full = os.path.join(d, fn)
            items.append({"name": fn, "path": os.path.abspath(full),
                          "size": os.path.getsize(full),
                          "is_photo": fn.lower().endswith(IMG_EXT)})
        return self._json({"dir": os.path.abspath(d), "items": items})

    def _thumb(self, q):
        p = (q.get("path") or [""])[0]
        ap = _safe_under(p, [UPLOAD_DIR, RUN_DIR, os.path.join(ROOT, "tmp"), ROOT])
        if not ap or not os.path.exists(ap):
            return self._send(404, b"no", "text/plain")
        if not ap.lower().endswith(IMG_EXT):
            return self._send(400, b"not image", "text/plain")
        try:
            from PIL import Image
            im = Image.open(ap)
            im = im.convert("RGB")
            im.thumbnail((360, 360))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=82)
            return self._send(200, buf.getvalue(), "image/jpeg")
        except Exception:
            with open(ap, "rb") as f:
                return self._send(200, f.read(),
                                  mimetypes.guess_type(ap)[0] or "application/octet-stream")

    def _recon(self, q):
        """台账 ↔ 最近一次出表产物对账。只读，不改台账也不联网。

        为什么让界面也能点：对账是在「出完表、心里没底」的时候做的，
        那时候人正在看页面，让他切窗口敲 python 十有八九就不做了。
        2026-11-23 定位变了之后，对账的含义也变了：以前核「点了直接入库有没有真进」，
        现在核「台账说已出过表的单，表是不是真出出来了」—— 对不上就说明台账记早了，
        下次重跑会被判 duplicate 跳过，那种漏才是最安静的一种。
        """
        try:
            import recon as recon_mod
        except Exception as e:
            return self._json({"ok": False, "error": "对账模块加载失败：%s" % e}, 500)
        d = (q.get("dir") or [""])[0]
        if d:
            ap = _safe_under(d, [OUT_ROOT, os.path.join(ROOT, "out")])
            if not ap:
                return self._json({"ok": False, "error": "批次目录不在允许范围内"}, 400)
            d = ap
        else:
            d = recon_mod.latest_dir()
        # 排序与来源过滤（用户 2026-11-25）：
        #   sort=at  记账时间倒序 —— 默认。台账首先是「我们哪天出的表」这本流水，
        #           出完表当天要核的就是这几单；按订单时间排会把三个月前的单顶上来。
        #   sort=order 订单时间倒序 —— 想按「货是哪天下的」看时切过来。
        #   origin   梅苗苗 / 牙e在线 / 其他（空 = 全部）。
        #            分三档而不是列平台名：认不出平台时来源会退成供货商名，
        #            按平台名过滤会把那些行整批漏掉（见 recon.origin_key）。
        sort = (q.get("sort") or ["at"])[0]
        if sort not in ("at", "order"):
            sort = "at"
        want = (q.get("origin") or [""])[0]
        if want and want not in recon_mod.ORIGIN_KEYS:
            want = ""
        docs, counts, files = recon_mod.load_decision(d or "")
        j = ledger.load()
        records = j.get("records") or []
        # 台账现在有两个含义不同的数字（用户 2026-11-25）：
        #   已入库 = 人导完美亚、回来点过「确认入库」的；
        #   未确认 = 表出出来了但没人确认导没导。
        # 只报一个总数会让人把「出了表」当成「进了库存」，这正是要消掉的混乱。
        n_ok = sum(1 for r in records if r.get("confirmed_at"))
        n_wait = len(records) - n_ok
        pairs, only_ledger, only_docs = recon_mod.match(records, docs)
        # 两侧都要「订单时间 + 来源」，补字段要看本地库 documents 全量
        # （台账里绝大多数单不在最近这一批里）。
        _db = recon_mod.db_index()
        only_ledger = recon_mod.decorate(only_ledger, docs, _db)
        only_docs = recon_mod.decorate(only_docs, docs, _db)
        # 「全部台账」这一份是给弹窗里那张表用的（用户 2026-11-25）。
        # 以前只列 only_ledger（台账有、本批没有），于是刚点过「确认入库」的那单
        # 恰好在本批里、被 match 归进 pairs，反而从表里消失了 —— 人点完确认
        # 回头一看台账里根本没这行，以为没记上。所以整张台账都要列出来，
        # 用 in_batch 标出「这一单也在本批表里」。
        all_rows = recon_mod.decorate(records, docs, _db)
        pair_keys = {ledger.norm_doc_no(r.get("doc_no")) for r, _d, _how in pairs}

        def _keep(x):
            if not want:
                return True
            return recon_mod.origin_key(x.get("origin_label")) == want

        # 过滤前先数好每档多少条：界面上的过滤按钮要显示「梅苗苗 12」这种数字，
        # 让人点之前就知道切过去有多少东西，点空了一个个试等于没做过滤。
        tally = {k: 0 for k in recon_mod.ORIGIN_KEYS}
        for r in all_rows:
            tally[recon_mod.origin_key(r.get("origin_label"))] += 1
        ledger_all = recon_mod.by_moment([r for r in all_rows if _keep(r)], sort)
        only_ledger = recon_mod.by_moment([r for r in only_ledger if _keep(r)], sort)
        only_docs = recon_mod.by_moment([r for r in only_docs if _keep(r)], sort)
        return self._json({
            "ok": True,
            "dir": d or "",
            "sort": sort,
            "origin": want,
            "origin_keys": list(recon_mod.ORIGIN_KEYS),
            "tally": tally,
            "inbound_file": files.get("inbound") or "",
            "products_file": files.get("products") or "",
            "doc_total": len(docs),
            "ledger_total": len(records),
            "ledger_confirmed": n_ok,
            "ledger_unconfirmed": n_wait,
            "pairs": [{"doc_no": r.get("doc_no"), "batch_doc": doc.get("doc_no"),
                       "amount": doc.get("amount"), "lines": doc.get("inbound"),
                       "how": how,
                       "order_date": recon_mod.order_date_of(r, doc),
                       "origin": recon_mod.origin_label(r, doc)} for r, doc, how in pairs],
            "only_ledger": [recon_mod.brief(r) for r in only_ledger],
            "ledger_all": [dict(recon_mod.brief(r),
                                in_batch=ledger.norm_doc_no(r.get("doc_no")) in pair_keys)
                           for r in ledger_all],
            "only_batch": [{"doc_no": doc.get("doc_no"), "amount": doc.get("amount"),
                            "lines": doc.get("inbound"), "state": doc.get("state"),
                            "origin": doc.get("origin_label") or "",
                            "order_date": doc.get("order_date") or ""}
                           for doc in only_docs],
        })

    def _confirm(self):
        """「确认入库」：人导完美亚的入库单，回这里点一下，台账才记「已入库」。

        为什么台账不能自己认这件事（用户 2026-11-25）：
            程序只出表，不负责导入。表出出来了、人却忘了导进美亚，是很常见的漏；
            以前台账在出表那一刻就记一笔，于是这批货在台账里「有」，在美亚库存里
            没有，月底对账差一批，而且查不出是哪批（美亚入库单没有源单据号）。
            把这个动作交回给人，台账才真的代表「进了库存的」。
        为什么只认台账里已有的单号：
            确认的是「我刚把那张表导了」，不是「凭空造一条入库记录」。台账里没有
            这个单号说明这批还没出过表（或链路上更早就断了），先出表再来确认。
        为什么带上 fingerprint 复核：
            同一单号重新出表（改过数量）以后，人确认的是新表还是旧表说不清。
            台账记录的指纹与当前单据不一致时，提示人先看一眼明细对不对。
        """
        j = json.loads(self._read_body() or b"{}")
        doc_no = str(j.get("doc_no") or "").strip()
        undo = bool(j.get("undo"))
        if not doc_no:
            return self._json({"error": "缺少单据号"}, 400)
        try:
            if undo:
                rec = ledger.unconfirm(doc_no)
                if rec is None:
                    return self._json({"ok": True, "doc_no": doc_no,
                                       "confirmed": False, "note": "这一单本来就没确认过"})
                return self._json({"ok": True, "doc_no": doc_no, "confirmed": False,
                                   "note": "已撤销「确认入库」：这单回到「表已出、未确认入库」"})
            rec, first = ledger.confirm(doc_no, note=(j.get("note") or "用户在界面上确认")[:200])
        except (ValueError, OSError) as e:
            return self._json({"error": "台账写不进去：%s" % e}, 500)
        if rec is None:
            return self._json({"error": "台账里没有这一单（%s）：先出表，再来确认入库"
                                        % doc_no}, 404)
        return self._json({"ok": True, "doc_no": doc_no, "confirmed": True,
                           "first": first, "at": rec.get("confirmed_at"),
                           "note": "已记下「已入库」。台账按记账时间排就是看这个标记"})

    def _download(self, q):
        p = (q.get("path") or [""])[0]
        ap = _safe_under(p, [OUT_ROOT, os.path.join(ROOT, "out"), os.path.join(ROOT, "tmp")])
        if not ap or not os.path.exists(ap):
            return self._send(404, b"no", "text/plain")
        ctype = mimetypes.guess_type(ap)[0] or "application/octet-stream"
        with open(ap, "rb") as f:
            body = f.read()
        name = os.path.basename(ap)
        # 文件名里带中文（入库表按供应商命名：inbound_四川牙易在线…xlsx）。
        # http.server 的发头是按 latin-1 编码的，直接把中文塞进 filename=""
        # 会在 write 头那一行抛 UnicodeEncodeError —— 客户端看到的是连接被中断
        # 或 500，而点链接的人只看到「下载失败」。所以按 RFC 5987 给
        # filename*=UTF-8''<百分号编码>，再给一个纯 ASCII 的 filename 兜底
        # （老浏览器只认后者，至少能存下来再改名）。
        ascii_name = name.encode("ascii", "replace").decode("ascii").replace("?", "_")
        ascii_name = re.sub(r"_{2,}", "_", ascii_name) or "download.xlsx"
        disp = "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
            ascii_name, urllib.parse.quote(name, safe=""))
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", disp)
        self.end_headers()
        self.wfile.write(body)

    def _stream(self):
        """SSE：先把 backlog 全量回放，再实时推。

        必须用 chunked：BaseHTTPRequestHandler 不会自动长度，而 SSE 不能有 Content-Length。
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(payload):
            data = ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")
            self.wfile.write(("%x\r\n" % len(data)).encode("ascii") + data + b"\r\n")
            self.wfile.flush()

        q = HUB.subscribe()
        try:
            with HUB.lock:
                backlog = list(HUB.backlog)
                state = HUB.state
                rc = HUB.rc
                out_now = HUB.out
            chunk({"kind": "hello", "state": state, "rc": rc,
                   "out": out_now, "backlog": backlog})
            last = time.time()
            while True:
                if q:
                    batch = q[:]
                    del q[:]
                    for rec in batch:
                        chunk(rec)
                    last = time.time()
                else:
                    if time.time() - last > 15:
                        chunk({"kind": "ping"})
                        last = time.time()
                    time.sleep(0.08)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            HUB.unsubscribe(q)

    def _upload(self):
        ctype = self.headers.get("Content-Type") or ""
        m = re.search(r"boundary=([^;]+)", ctype)
        if not m:
            return self._json({"error": "不是 multipart 请求"}, 400)
        boundary = m.group(1).strip('"').encode("ascii")
        body = self._read_body()
        parts = parse_multipart(body, boundary)
        batch = os.path.join(UPLOAD_DIR, time.strftime("%Y%m%d_%H%M%S"))
        os.makedirs(batch, exist_ok=True)
        saved = []
        for p in parts:
            fn = os.path.basename(p["filename"] or "")
            if not fn or not p["data"]:
                continue
            if not fn.lower().endswith(SUPPORTED):
                continue
            dst = os.path.join(batch, fn)
            # 同名覆盖会让用户以为传了两张，其实只剩一张
            i = 1
            while os.path.exists(dst):
                stem, ext = os.path.splitext(fn)
                dst = os.path.join(batch, "%s_%d%s" % (stem, i, ext))
                i += 1
            with open(dst, "wb") as f:
                f.write(p["data"])
            saved.append({"name": os.path.basename(dst), "path": os.path.abspath(dst),
                          "size": len(p["data"]),
                          "is_photo": dst.lower().endswith(IMG_EXT)})
        return self._json({"dir": batch, "files": saved})

    def _import_library(self):
        """把用户在美亚「产品库 → 批量导出」拿到的 xlsx 合并进本地库。

        为什么要这个通道：本地库没有的物品，程序给不出物品编号（编号是美亚
        建档之后才发的），只能出建档表。人要拿着建档表去美亚建号，建完必须
        把「建好的结果」带回来，否则下一批同样判「先建档」，白跑一轮。

        **这不是读美亚数据**：文件是人自己在美亚界面点导出得到的，程序只读
        本地磁盘上这一个文件，一个网络请求都不发（no_meiya 在 socket 层拦着）。
        所以这里走 multipart 上传，和图片上传同一套解析。
        """
        ctype = self.headers.get("Content-Type") or ""
        m = re.search(r"boundary=([^;]+)", ctype)
        if not m:
            return self._json({"error": "不是 multipart 请求"}, 400)
        boundary = m.group(1).strip('"').encode("ascii")
        parts = parse_multipart(self._read_body(), boundary)
        pick = None
        for p in parts:
            fn = os.path.basename(p["filename"] or "")
            if fn.lower().endswith(SHEET_EXT) and p["data"]:
                pick = (fn, p["data"])
                break
        if not pick:
            return self._json({"error": "没收到表格文件（只认 xlsx/xls/xlsm）"}, 400)
        batch = os.path.join(UPLOAD_DIR, "lib_" + time.strftime("%Y%m%d_%H%M%S"))
        os.makedirs(batch, exist_ok=True)
        dst = os.path.join(batch, pick[0])
        with open(dst, "wb") as f:
            f.write(pick[1])
        r = localdb.import_export(dst)
        r["file"] = os.path.abspath(dst)
        r["name"] = pick[0]
        return self._json(r, 200 if r.get("ok") else 400)

    def _probe(self):
        """试识别：只跑 ocr_photo.extract，不写 out、不建档、不入库。"""
        j = json.loads(self._read_body() or b"{}")
        path = j.get("path") or ""
        profile = j.get("profile") or ""
        if not (path and profile):
            return self._json({"error": "path 与 profile 都必须给"}, 400)
        if not os.path.exists(path):
            return self._json({"error": "文件不存在：%s" % path}, 400)
        cmd = [python_bin(), os.path.join(TOOLS, "run.py"),
               "--probe", path, "--profile", profile]
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600)
        out = (p.stdout or "").strip().splitlines()
        data = None
        for line in reversed(out):
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except ValueError:
                    continue
        if data is None:
            return self._json({"error": "试识别失败",
                               "stderr": (p.stderr or "")[-1200:],
                               "rc": p.returncode}, 200)
        return self._json({"ok": True, "result": data})

    def _run(self):
        j = json.loads(self._read_body() or b"{}")
        if HUB.state == "running":
            return self._json({"error": "已有任务在跑，先停止或等它结束"}, 409)
        mode = j.get("mode") or "files"          # pull / files / photos
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.join(OUT_ROOT, ts)
        os.makedirs(out_dir, exist_ok=True)
        cmd = [python_bin(), os.path.join(TOOLS, "pipeline.py"),
               "--out", out_dir, "--evt"]
        inner_base = int(j.get("inner_base") or 92000)
        cmd += ["--inner-base", str(inner_base)]

        plan = None
        if mode == "pull":
            plat = j.get("platform")
            if plat not in ("mmm", "yae"):
                return self._json({"error": "平台必须选 mmm 或 yae"}, 400)
            d1, d2 = j.get("from"), j.get("to")
            if not (d1 and d2):
                return self._json({"error": "拉单必须给日期区间"}, 400)
            # 起止反了就直接拒，不要交给平台去猜。
            # 平台对 from > to 的反应是「成功拉到 0 单」，界面显示 0 份单据、
            # 一切正常 —— 少一个月的单看不出来。整月模式是页面算出来的，
            # 但「按区间」是人手填的，这道闸就架在这。
            for _d in (d1, d2):
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(_d)):
                    return self._json({"error": "日期格式应为 YYYY-MM-DD：%s" % _d}, 400)
            if str(d1) > str(d2):
                return self._json({"error": "开始日期 %s 晚于结束日期 %s" % (d1, d2)}, 400)
            cmd += ["--pull", plat, "--pull-from", d1, "--pull-to", d2]
        else:
            src_dir = j.get("dir") or ""
            if mode == "photos":
                picked = [x for x in (j.get("photos") or []) if x.get("include")]
                if not picked:
                    return self._json({"error": "没有勾选任何照片"}, 400)
                stage = os.path.join(RUN_DIR, ts)
                os.makedirs(stage, exist_ok=True)
                plan = {}
                used_names = set()
                for ph in picked:
                    p = ph.get("path") or ""
                    if not os.path.exists(p):
                        return self._json({"error": "照片不存在：%s" % p}, 400)
                    name = os.path.basename(p)
                    base, ext = os.path.splitext(name)
                    i = 1
                    while name in used_names:
                        name = "%s_%d%s" % (base, i, ext)
                        i += 1
                    used_names.add(name)
                    dst = os.path.join(stage, name)
                    shutil.copy2(p, dst)
                    plan[os.path.abspath(dst)] = ph.get("profile") or None
                src_dir = stage
            if not src_dir or not os.path.isdir(src_dir):
                return self._json({"error": "目录不存在：%s" % src_dir}, 400)
            cmd += ["--in", src_dir]
            if plan is not None:
                pp = os.path.join(out_dir, "_photo_plan.json")
                with open(pp, "w", encoding="utf-8") as f:
                    json.dump(plan, f, ensure_ascii=False)
                cmd += ["--photo-plan", pp]

        # supplier / commit 都不再传：本程序不连美亚，出表就是终点。
        # 老表单（或存下来的 _cmd.json）仍可能带这两个字段，忽略即可 ——
        # 报错只会让翻历史批次时莫名其妙失败。

        log_path = os.path.join(out_dir, "run.log")
        # 把这次的命令行原样存下来。用途只有一个，但很关键：
        # 「效期不足」的行需要用户点头后才放行，点头后必须用**同一批数据、同一个输出目录**
        # 重跑（加 --approve-expiry）。不存命令行的话，界面重跑只能靠人重新填一遍表单，
        # 而重填最容易出的错是换了目录或换了日期区间，于是审批对不上原来那批。
        try:
            with open(os.path.join(out_dir, "_cmd.json"), "w", encoding="utf-8") as f:
                json.dump({"cmd": cmd, "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                          f, ensure_ascii=False, indent=1)
        except OSError:
            pass
        HUB.out = out_dir
        t = threading.Thread(target=_run_pipeline, args=(cmd, log_path), daemon=True)
        t.start()
        return self._json({"started": True, "out": out_dir, "log": log_path})

    def _resolve(self):
        """「保存并继续」：把人在弹窗上做的处置写下去，然后用**同一批数据、同一个目录**接着跑。

        为什么要一个合并接口，而不是让页面先调 /api/approve 再调 /api/fixes：
        两次调用之间只要有一次失败（或用户按了两次），就会出现「审批写了一半就重跑」，
        结果是一半放行、一半仍被拦，而界面看起来跟上次一模一样 —— 人会以为按钮没生效。
        这里把所有改动收在同一处、串行执行，最后只点火一次。

        为什么每一条都要拿磁盘上的 pipeline_summary.json 复核：
        弹窗是几秒前渲染的，这期间单号/物品行可能已经变了；只按前端传来的键写配置，
        写进去对不上任何人，界面上一按「继续」却什么都没变（静默失效）。
        所以前端只负责「选了哪几条」，有效期、分类这类事实仍从磁盘取。
        """
        j = json.loads(self._read_body() or b"{}")
        out = os.path.abspath(j.get("out") or "")
        if not _safe_under(out, [OUT_ROOT, os.path.join(ROOT, "out")]):
            return self._json({"error": "批次目录不在允许范围内"}, 400)
        if HUB.state == "running":
            return self._json({"error": "已有任务在跑，先等它结束"}, 409)
        summ_path = os.path.join(out, "pipeline_summary.json")
        if not os.path.exists(summ_path):
            return self._json({"error": "这批还没有结果文件，先跑一轮"}, 400)
        with open(summ_path, encoding="utf-8") as f:
            summ = json.load(f)
        docs = {str(d.get("doc_no") or "").strip(): d
                for d in (summ.get("documents") or []) if d.get("doc_no")}

        import build_import          # 只为校验分类值，避免写进非法字面量
        n_ok = n_skip = 0
        skipped = []
        note = (j.get("note") or "用户在界面上处置")[:200]

        # ── 1) 效期不足：同意入库 ──────────────────────────────────
        for it in (j.get("approve") or []):
            doc_no = str(it.get("doc_no") or "").strip()
            name = str(it.get("item") or it.get("物品名称") or "").strip()
            doc = docs.get(doc_no)
            if not doc or not name:
                n_skip += 1
                skipped.append("%s / %s（结果里找不到）" % (doc_no or "?", name or "?"))
                continue
            det = doc.get("detail") if isinstance(doc.get("detail"), dict) else {}
            hit = next((x for x in (det.get("items") or [])
                        if str(x.get("物品名称") or "").strip() == name), None)
            if not hit:
                # 没有这一行说明它已经被别的处置放行/拦下了。不报错，只记下来 ——
                # 这时候人点的是「继续」，不是「排查」，弹窗报红只会挡住他。
                n_skip += 1
                skipped.append("%s / %s（这批已经不需要审批了）" % (doc_no, name))
                continue
            expiry_mod.approve(doc_no, name, hit.get("有效期"), reason=note, by="ui")
            n_ok += 1

        # ── 2) 缺信息：人工补一级分类 / 拆包 / 这批不入 ─────────────
        for fx in (j.get("fixes") or []):
            doc_no = str(fx.get("doc_no") or "").strip()
            name = str(fx.get("item") or fx.get("物品名称") or "").strip()
            spec = str(fx.get("spec") or fx.get("规格") or "").strip()
            doc = docs.get(doc_no)
            if not doc or not name:
                n_skip += 1
                skipped.append("%s / %s（结果里找不到）" % (doc_no or "?", name or "?"))
                continue
            l1 = str(fx.get("l1") or "").strip()
            if l1 and l1 not in build_import.ALLOWED_L1:
                n_skip += 1
                skipped.append("%s / %s（分类「%s」不是分类树里的值）" % (doc_no, name, l1))
                continue
            kw = {"by": "ui", "reason": note}
            if l1:
                kw["l1"] = l1
            if fx.get("pack_qty"):
                kw["pack_qty"] = fx.get("pack_qty")
            if fx.get("pack_unit"):
                kw["pack_unit"] = fx.get("pack_unit")
            if fx.get("skip"):
                kw["skip"] = True
            manual_fixes_mod.set_fix(doc_no, name, spec, **kw)
            n_ok += 1

        # ── 3) 档案歧义：指定用哪一条物品编号 ─────────────────────
        # 歧义行不进建档表（那是给美亚还没有的物品用的），唯一出口是把人选中的
        # 物品编号记进 config/inbound_overrides.json，重跑时按身份取号。
        for pk in (j.get("picks") or []):
            doc_no = str(pk.get("doc_no") or "").strip()
            name = str(pk.get("item") or pk.get("物品名称") or "").strip()
            spec = str(pk.get("spec") or pk.get("规格") or "").strip()
            ident = str(pk.get("ident") or "").strip()
            item_no = str(pk.get("item_no") or "").strip()
            doc = docs.get(doc_no)
            if not doc or not name or not item_no:
                n_skip += 1
                skipped.append("%s / %s（结果里找不到或没选编号）" % (doc_no or "?", name or "?"))
                continue
            # 候选必须来自磁盘上这份结果：前端传来的编号若不在候选里，
            # 说明它过期了（批次换过 / 人改了别处），写进去会指定到不存在的物品。
            det = doc.get("detail") if isinstance(doc.get("detail"), dict) else {}
            hit = next((x for x in (det.get("items") or [])
                        if str(x.get("物品名称") or "").strip() == name
                        and str(x.get("规格") or "").strip() == spec), None)
            allowed = {str(c.get("item_no") or "").strip()
                       for c in ((hit or {}).get("cands") or [])}
            if not hit or item_no not in allowed:
                n_skip += 1
                skipped.append("%s / %s（%s 不在候选档案里）" % (doc_no, name, item_no))
                continue
            ident = str(hit.get("ident") or ident).strip()
            if not ident:
                n_skip += 1
                skipped.append("%s / %s（这条没有身份键，无法记录）" % (doc_no, name))
                continue
            build_import.add_override(ident, item_no)
            n_ok += 1

        started = None
        if j.get("resume") and (n_ok or j.get("force")):
            started = self._resume(out)
        return self._json({"ok": True, "saved": n_ok, "skipped": n_skip,
                           "skipped_items": skipped[:20],
                           "started": bool(started), "out": out,
                           "note": self._resolve_note(n_ok, n_skip, started, skipped)})

    @staticmethod
    def _resolve_note(n_ok, n_skip, started, skipped):
        # 提示语把「还没处理的会被继续拦」说清楚：用户以为按下继续就全都放行了，
        # 下一轮又看到同样的红行，就会怀疑按钮是假的。
        parts = []
        if n_ok:
            parts.append("已记下 %d 项处置" % n_ok)
        if n_skip:
            parts.append("%d 项没写进去（%s）" % (n_skip, "；".join(skipped[:3])))
        if started:
            parts.append("正在用同一批数据重新跑一遍；还没处置的仍会被拦下")
        elif not n_ok:
            parts.append("没有要写的东西")
        return "；".join(parts) if parts else "已保存"

    def _resume(self, out):
        """按 <批次>/_cmd.json 里存的原命令行，用同一个输出目录再跑一遍。

        为什么读磁盘上的命令行而不是让前端重发：重跑必须落在**同一个 out 目录**。
        界面重填一遍表单，最容易变的正是目录（时间戳是新的），于是刚写的审批
        和上一轮的单号对不上，表现为「审了还是不认」。
        """
        p = os.path.join(out, "_cmd.json")
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                saved = json.load(f) or {}
        except (OSError, ValueError):
            return None
        cmd = list(saved.get("cmd") or [])
        if not cmd:
            return None
        cmd = _resume_cmd(cmd, saved.get("src"))
        log_path = os.path.join(out, "run.log")
        HUB.out = out
        t = threading.Thread(target=_run_pipeline, args=(cmd, log_path, "a"), daemon=True)
        t.start()
        return out

    def _approve(self):
        """「同意入库」：只登记效期审批，**不启动任何子进程**。

        为什么不接受前端传行清单：前端传的清单可能过期（期间单据改过），
        而审批键是「单据号 + 物品名 + 有效期」，一旦对不上就会静默不生效——
        表现为「我点了同意，它还是拦着」，比没有按钮更让人困惑。
        所以这里只信磁盘上的 pipeline_summary.json，那是程序刚算出来的事实。
        """
        j = json.loads(self._read_body() or b"{}")
        out = os.path.abspath(j.get("out") or "")
        if not _safe_under(out, [OUT_ROOT, os.path.join(ROOT, "out")]):
            return self._json({"error": "批次目录不在允许范围内"}, 400)
        if HUB.state == "running":
            return self._json({"error": "已有任务在跑，先等它结束"}, 409)
        # 不再要求 _cmd.json：这个接口现在只写审批文件，不启动任何子进程。
        # 之前要求它是因为要点火重跑，现在没这个必要了（用户 2026-11-23 第 1 条反馈）。
        # 只登记审批，**不启动任何流程**。
        # 血案（用户 2026-11-23 第 1 条反馈）：原来这里直接把整批重跑一遍，
        # 而重跑是完整的 pipeline（抽取→回填→点「直接入库」），于是「点同意」
        # 等于「批准 + 立刻入库」——用户只想批准，不想顺手下单。
        # 现在点同意只写 config/expiry_approvals.json，入库要人再按「开始」。
        summ_path = os.path.join(out, "pipeline_summary.json")
        if not os.path.exists(summ_path):
            return self._json({"error": "这批还没有结果文件，先跑一轮"}, 400)
        with open(summ_path, encoding="utf-8") as f:
            summ = json.load(f)
        n = 0
        note = (j.get("note") or "用户在界面上同意入库")[:200]
        for d in summ.get("documents") or []:
            det = d.get("detail") if isinstance(d.get("detail"), dict) else {}
            for it in det.get("items") or []:
                expiry_mod.approve(d.get("doc_no"), it.get("物品名称"),
                                   it.get("有效期"), reason=note, by="ui")
                n += 1
        return self._json({"ok": True, "approved": n,
                           "note": "已登记 %d 行效期审批。入库不会自动开始，"
                                   "确认后点「开始」重跑本批即可放行。" % n})

    def _reject(self):
        """「禁止入库」：把某一行（或整张单）记成永久不入库（赠品、样品、客户自备料）。

        带 item 就只标那一行：一张单里常常只有一行是赠品/客户自备料，
        而以前接口只认单据号，点一次就把整单所有行都标了 —— 用户实测
        「只想禁止审批的这一个，结果 5 行全被标」（2026-11-25 投诉）。
        不带 item 才是「整单都不该入」，这个语义保留（老批次、老脚本仍在用）。
        """
        j = json.loads(self._read_body() or b"{}")
        out = os.path.abspath(j.get("out") or "")
        if not _safe_under(out, [OUT_ROOT, os.path.join(ROOT, "out")]):
            return self._json({"error": "批次目录不在允许范围内"}, 400)
        stem = (j.get("stem") or "").strip()
        undo = bool(j.get("undo"))
        summ_path = os.path.join(out, "pipeline_summary.json")
        if not (stem and os.path.exists(summ_path)):
            return self._json({"error": "找不到这张单的结果"}, 400)
        with open(summ_path, encoding="utf-8") as f:
            summ = json.load(f)
        doc = next((d for d in (summ.get("documents") or [])
                    if (d.get("stem") or "") == stem), None)
        if not doc:
            return self._json({"error": "结果里没有 %s" % stem}, 400)
        # 行名必须从抽取结果里取：结果表里只有单据级信息，
        # 而「禁止入库」是按行记的（同一张单可能只有一行是赠品）。
        names = list(doc.get("line_names") or [])
        if not names:
            ex = doc.get("extracted") or ""
            if ex and os.path.exists(ex):
                with open(ex, encoding="utf-8") as f:
                    names = [ln.get("product_name")
                             for ln in (json.load(f).get("lines") or [])]
        names = [x for x in names if x]
        if not names:
            return self._json({"error": "这张单没有可用行名（抽取结果已清理），"
                                        "请在命令行 python tools/reject.py add 单据号 物品名"}, 400)
        # 指定了行就只动这一行。行名必须能在本单里找到，否则会写一条
        # 谁也匹配不上的记录 —— 界面显示「已标记」，下一轮那行照样进表。
        only = str(j.get("item") or j.get("物品名称") or "").strip()
        if only:
            if only not in names:
                return self._json({"error": "这张单里没有「%s」这一行（可能是页面过期，"
                                            "刷新后重试）" % only}, 400)
            names = [only]
        n = 0
        for nm in names:
            if undo:
                n += 1 if reject_mod.unreject(doc.get("doc_no"), nm) else 0
            else:
                reject_mod.reject(doc.get("doc_no"), nm,
                                  reason=(j.get("note") or "用户在界面上标记禁止入库")[:200],
                                  by="ui")
                n += 1
        what = ("「%s」这一行" % only) if only else "这张单的 %d 行" % n
        return self._json({"ok": True, "count": n, "undo": undo, "item": only,
                           "note": ("已撤销 %s 的禁止标记，点「开始」重跑后生效" % what) if undo
                                   else ("已把 %s 标记为禁止入库，点「开始」重跑后生效" % what)})

    def _stop(self):
        proc = HUB.proc
        if not proc:
            return self._json({"ok": False, "why": "当前没有在跑的任务"})
        _kill_tree(proc)
        HUB.state = "stopped"
        HUB.publish("line", text="[ui] 已请求停止（已建档/已上传的部分不会回滚）")
        return self._json({"ok": True})


def main():
    # line_buffering=True 不是可有可无的：重定向到文件/管道时 TextIOWrapper 默认是
    # 块缓冲（8KB），进度行会一直压在缓冲里——界面看起来「卡住不动」，日志文件是空的。
    # PYTHONUNBUFFERED=1 救不了这一层，它只影响 Python 启动时的 sys.stdout。
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--open", action="store_true",
                    help="启动后在带调试端口的 Edge 里打开页面（同一个窗口既看界面也能拉单）")
    ap.add_argument("--no-browser", action="store_true",
                    help="不自动开浏览器（调试界面自身时用，免得每次重启多开一个标签）")
    ap.add_argument("--open-page", metavar="URL", default="",
                    help="只做一件事：确保调试浏览器在跑，并在它里面打开这个地址，然后退出"
                         "（启动器发现助手已经在运行时用它，避免另开一个没有调试端口的窗口）")
    a = ap.parse_args()
    if a.open_page:
        # 单独一条腿的理由：启动器第二次双击时助手已经在跑，这时**不能**再调普通
        # Edge 开页面 —— 那会开出一个没有调试端口的窗口，界面上永远显示「浏览器没带
        # 调试端口」，用户看到的是两个窗口。必须复用带调试端口的那个实例。
        tid = open_page_in_debug_browser(a.open_page)
        return 0 if tid else 1
    _ensure_dirs()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = "http://%s:%d/" % (a.host, a.port)
    print("美亚入库助手 V1.0 -> %s" % url)
    print("（只监听本机；Ctrl+C 退出）")
    if a.open:
        # 关键：开的必须是**带调试端口**的那个 Edge，而不是随便一个 Edge 窗口。
        # 以前这里走 open_page_in_edge()，开出的是普通 profile 的窗口（无调试端口），
        # 界面一加载就显示「浏览器没带调试端口」，用户只好再点「打开浏览器」——
        # 于是屏幕上两个窗口，一个连着一个不连，人分不清该在哪个里登录。
        # 现在启动时就把调试浏览器拉起来，界面页也开在它里面，一个窗口全解决。
        threading.Timer(0.8, lambda: open_page_in_debug_browser(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        _kill_tree(HUB.proc)
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
