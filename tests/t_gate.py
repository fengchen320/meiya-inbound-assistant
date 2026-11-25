# -*- coding: utf-8 -*-
"""禁令闸门回归（替代已废弃的 t_commit_judge.py）。

跑法：python tmp/t_gate.py

为什么这个测试重要：定位变更后，最危险的不是「功能不好用」，而是某天有人
（人或 AI）把美亚调用加回来。代码闸门在 socket 层，这里验证三件事：
  1) 连 myyun 任意子域都会被拦（含 api. / www.）；
  2) 别的域名不受影响（别把电商平台也拦死）；
  3) tools/ 下的运行代码里没有 import lib_meiya、也没有引用 cdp_*.mjs。
"""
import io, sys, os, re, socket, glob
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import no_meiya

ok = True
def chk(label, got, want):
    global ok
    good = got == want
    ok = ok and good
    print("%-34s %-22s %s" % (label, str(got)[:22], "OK" if good else "WANT " + str(want)))

# 1) 三个子域都必须拦
for host in ("myyun.com", "api.myyun.com", "www.myyun.com"):
    try:
        socket.create_connection((host, 443), timeout=3)
        blocked = False
    except no_meiya.MeiyaForbidden:
        blocked = True
    except Exception as ex:                      # DNS 之类不算「放行」
        blocked = "other:" + type(ex).__name__
    chk("block " + host, blocked, True)

# 2) 正常域名不能被误拦（DNS 解析失败也算「没被闸门拦」）
try:
    socket.create_connection(("example.com", 443), timeout=3)
    other = "connected"
except no_meiya.MeiyaForbidden:
    other = "BLOCKED"
except Exception as ex:
    other = "not-blocked:" + type(ex).__name__
chk("example.com 不被拦", other == "BLOCKED", False)

# 3) 静态扫描：运行代码里不许再出现美亚调用
TOOLS = os.path.join(ROOT, "tools")
bad = []
for p in glob.glob(os.path.join(TOOLS, "*.py")):
    if os.path.basename(p) in ("no_meiya.py",):
        continue
    src = io.open(p, encoding="utf-8", errors="replace").read()
    if re.search(r"^\s*import\s+lib_meiya|^\s*from\s+lib_meiya", src, re.M):
        bad.append(os.path.basename(p) + ":import lib_meiya")
    # 只认「带引号的字面量」＝真会被执行的路径；docstring 里提一句不算（lib_mmm 就只在注释里提）
    if re.search(r"""["']cdp_(login|inbound|token|upload)\.mjs""", src):
        bad.append(os.path.basename(p) + ":cdp_*.mjs")
chk("运行代码无美亚调用", bad, [])

# 4) tools 根下不许再有 lib_meiya（历史备份目录 _legacy_meiya 不随开源仓库发布）
chk("tools 根无 lib_meiya", os.path.exists(os.path.join(TOOLS, "lib_meiya.py")), False)

print("ALL_OK", ok)
sys.exit(0 if ok else 1)
