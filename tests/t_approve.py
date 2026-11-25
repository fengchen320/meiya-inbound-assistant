# -*- coding: utf-8 -*-
"""审批必须真的放行 + 重跑不许重新拉单（2026-11-24，用户 m13168）。

跑法：python tmp/t_approve.py

用户原话：「审批后不需要重新从平台拉取订单数据啊…BUG：审批后点击保存继续后会
一直重新拉取订单处理后又让审核，无限循环」。两个根因：

  1. emit.plan() 判效期时不查 config/expiry_approvals.json，人在界面上点了
     「同意入库」、文件也写了，出表这一步照样判 awaiting_approval；
  2. ui._resume() 原样重放 _cmd.json 里带 --pull 的命令，每次都重新拉一遍单。
"""
import io, json, os, shutil, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import localdb, emit, expiry, ui

OUT = os.path.join(ROOT, "out", "emit", "t_approve")
if os.path.isdir(OUT):
    shutil.rmtree(OUT)
DB = os.path.join(ROOT, "tmp", "t_approve.db")
if os.path.exists(DB):
    os.remove(DB)
localdb.init(DB)
conn = localdb.connect(DB)

# 审批文件单独放，绝不碰真实的 config/expiry_approvals.json
APV = os.path.join(ROOT, "tmp", "t_approve_approvals.json")
if os.path.exists(APV):
    os.remove(APV)

ok = True


def chk(label, got, want):
    global ok
    good = got == want
    ok = ok and good
    print("%-32s %-30s %s" % (label, got, "OK" if good else "WANT " + str(want)))


DOC_NO = "TESTAPV0001"
ITEM = "测试 近效期耗材"
EXP = "2027-01-01"          # 距单据日期 2026-04-01 只有 275 天
path = os.path.join(os.path.join(ROOT, "tmp"), DOC_NO + "_extracted.json")
json.dump({"document": {"doc_no": DOC_NO, "doc_date": "2026-04-01",
                        "supplier_raw": "测试供应商甲"},
           "lines": [{"product_name": ITEM, "spec": "1支/盒", "brand": "测试牌",
                      "license_no": "川械注准20259401", "unit": "盒", "quantity": 1,
                      "unit_price": 10.0, "amount": 10.0, "lot_no": "L1",
                      "expiry_date": EXP, "manufacturer": "测试厂"}]},
          open(path, "w", encoding="utf-8"), ensure_ascii=False)

# 先记进本地库并给物品编号：不然它会被判 need_build，测不出效期这条路径
emit.emit([path], os.path.join(OUT, "warm"), conn=conn)
conn.execute("UPDATE products SET item_no=?, status='built' WHERE name=?", ("10901", ITEM))
conn.commit()

# ── 1. 未审批：必须拦在 awaiting_approval，入库行 0 ──
r1 = emit.emit([path], os.path.join(OUT, "a"), conn=conn)
chk("未审批 -> 状态", r1["docs"][0]["state"], emit.ST_AWAITING)
chk("未审批 -> 入库行", len(r1["docs"][0]["inbound_rows"]), 0)
chk("未审批 -> 审批项", len(r1["docs"][0]["approval"]), 1)

# ── 2. 审批后重跑：同一份数据必须直接放行（这就是死循环的根因）──
expiry.approve(DOC_NO, ITEM, EXP, reason="单测：用户同意入库", by="ui", path=APV)
# emit.plan 走的是 expiry 模块内的默认路径，这里临时指过去
_orig = expiry.APPROVALS
expiry.APPROVALS = APV
try:
    r2 = emit.emit([path], os.path.join(OUT, "b"), conn=conn)
finally:
    expiry.APPROVALS = _orig
chk("已审批 -> 状态", r2["docs"][0]["state"], emit.ST_READY)
chk("已审批 -> 入库行", len(r2["docs"][0]["inbound_rows"]), 1)
chk("已审批 -> 不再要求审批", len(r2["docs"][0]["approval"]), 0)
chk("已审批 -> 留了放行记录", len(r2["docs"][0]["approved"]), 1)

# ── 3. 重跑命令不许再带 --pull ──
# src 用一个临时目录：_resume_cmd 只在目录确实存在时才改写成 --in（宁可报错也不猜）。
SRC = os.path.join(TMP, "pull_yae")
os.makedirs(SRC, exist_ok=True)
cmd = ["py", "pipeline.py", "--out", OUT, "--evt", "--inner-base", "92000",
       "--pull", "yae", "--pull-from", "2026-04-01", "--pull-to", "2026-04-30"]
got = ui._resume_cmd(cmd, SRC)
chk("重跑 -> 不带 --pull", "--pull" in got, False)
chk("重跑 -> 不带 --pull-from", "--pull-from" in got, False)
chk("重跑 -> 改用 --in", got[-2:], ["--in", os.path.abspath(SRC)])
chk("重跑 -> 保留 out 目录", "--out" in got, True)
# 不是拉单来的批次（本来就 --in），命令必须原样不动
plain = ["py", "pipeline.py", "--out", OUT, "--in", os.path.join(ROOT, "out", "ui", "x")]
chk("非拉单批次原样返回", ui._resume_cmd(plain, ""), plain)

for f in (path,):
    if os.path.exists(f):
        os.remove(f)
if os.path.exists(APV):
    os.remove(APV)

print("ALL_OK", ok)
sys.exit(0 if ok else 1)
