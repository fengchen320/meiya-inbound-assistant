# -*- coding: utf-8 -*-
"""待审批先不出建档表 + 单行禁止入库（2026-11-25，用户 m13583）。

用户原话：「1、如发现需要审批的必须审批后才进行后一步建档表
2、一个订单只有某一个物品禁止入库时提示5行全部标记禁止入库，
实际只想禁止审批的这一个禁止入库」。

跑法：python tmp/t_holdline.py
"""
import io, json, os, shutil, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import localdb, emit, expiry, reject as reject_mod

OUT = os.path.join(ROOT, "out", "emit", "t_holdline")
if os.path.isdir(OUT):
    shutil.rmtree(OUT)
DB = os.path.join(ROOT, "tmp", "t_holdline.db")
if os.path.exists(DB):
    os.remove(DB)
localdb.init(DB)
conn = localdb.connect(DB)

APV = os.path.join(ROOT, "tmp", "t_holdline_approvals.json")
REJ = os.path.join(ROOT, "tmp", "t_holdline_rejects.json")
for f in (APV, REJ):
    if os.path.exists(f):
        os.remove(f)

ok = True
def chk(label, got, want):
    global ok
    good = got == want
    ok = ok and good
    print("%-34s %-30s %s" % (label, got, "OK" if good else "WANT " + str(want)))

DOC_NO = "TESTHOLD0001"
NEAR = "测试 近效期甲"      # 效期不足 -> 待审批（已建档，审批后能直接入）
LATER = "测试 远效期乙"     # 效期正常但没有物品编号 -> 待建档
path = os.path.join(os.path.join(ROOT, "tmp"), DOC_NO + "_extracted.json")
json.dump({"document": {"doc_no": DOC_NO, "doc_date": "2026-04-01",
                        "supplier_raw": "测试供应商甲"},
           "lines": [
               {"product_name": NEAR, "spec": "1支/盒", "brand": "测试牌",
                "license_no": "川械注准20259501", "unit": "盒", "quantity": 1,
                "unit_price": 10.0, "amount": 10.0, "lot_no": "L1",
                "expiry_date": "2027-01-01", "manufacturer": "测试厂"},
               {"product_name": LATER, "spec": "2支/盒", "brand": "测试牌",
                "license_no": "川械注准20259502", "unit": "盒", "quantity": 1,
                "unit_price": 20.0, "amount": 20.0, "lot_no": "L2",
                "expiry_date": "2030-01-01", "manufacturer": "测试厂"}]},
          open(path, "w", encoding="utf-8"), ensure_ascii=False)

# 先跑一轮把它们记进本地库，再给「甲」补物品编号（模拟已建档）
emit.emit([path], os.path.join(OUT, "warm"), conn=conn)
conn.execute("UPDATE products SET item_no=?, status='built' WHERE name=?", ("10901", NEAR))
conn.commit()

# ── 1. 有待审批行时，建档表里不许出现这单的新物品 ──
r1 = emit.emit([path], os.path.join(OUT, "a"), conn=conn)
d1 = r1["docs"][0]
chk("状态", d1["state"], emit.ST_AWAITING)
chk("挂起的建档项（只有乙）", r1["counts"]["products_held"], 1)
chk("建档表行数（应为 0）", r1["counts"]["products"], 0)
chk("建档表文件不存在", os.path.exists(os.path.join(OUT, "a", "products.xlsx")), False)
chk("单据级 products_held", d1["products_held"], 1)

# 号已经发出去了（记进本地库 pending），下一轮必须沿用，不能再发新号
rows = conn.execute("SELECT name, inner_no FROM products ORDER BY id").fetchall()
inner_before = {r["name"]: r["inner_no"] for r in rows}
chk("挂起物品已入库待建档", len(inner_before), 2)

# ── 2. 审批通过后：甲进库表，乙才进建档表 ──
expiry.approve(DOC_NO, NEAR, "2027-01-01", reason="单测", by="ui", path=APV)
_orig = expiry.APPROVALS
expiry.APPROVALS = APV
try:
    r2 = emit.emit([path], os.path.join(OUT, "b"), conn=conn)
finally:
    expiry.APPROVALS = _orig
d2 = r2["docs"][0]
chk("审批后状态", d2["state"], emit.ST_NEED_BUILD)
chk("审批后：入库行是甲", [x[0] for x in d2["inbound_rows"]], [NEAR])
chk("审批后：建档项只剩乙", [x["物品名称"] for x in d2["need_build"]], [LATER])
chk("审批后：建档表 1 行", r2["counts"]["products"], 1)
chk("审批后：挂起 0", r2["counts"]["products_held"], 0)

# ── 3. 单行禁止：只禁乙，甲必须还在入库行里（这是用户第 2 条诉求）──
reject_mod.reject(DOC_NO, LATER, reason="单测：只禁这一行", by="ui", path=REJ)
_orig_rej = reject_mod.REJECTS
reject_mod.REJECTS = REJ
# 审批文件也要继续指着临时文件：不然甲又变回「待审批」，这一轮根本不会有入库行，
# 就测不出「只禁乙、甲照入」这件最关键的事。
expiry.APPROVALS = APV
try:
    r3 = emit.emit([path], os.path.join(OUT, "c"), conn=conn)
finally:
    reject_mod.REJECTS = _orig_rej
    expiry.APPROVALS = _orig
d3 = r3["docs"][0]
chk("禁乙后：甲仍在入库行", [x[0] for x in d3["inbound_rows"]], [NEAR])
chk("禁乙后：被禁只有乙", [x["物品名称"] for x in (d3.get("rejected") or [])], [LATER])
chk("禁乙后：不用再建档", d3["need_build"], [])
# 另一行（甲）没被误禁 —— 老行为是点一次整单全标
chk("甲没被误禁", reject_mod.is_rejected(DOC_NO, NEAR, path=REJ), False)
chk("乙被禁", reject_mod.is_rejected(DOC_NO, LATER, path=REJ), True)

# ── 4. 内部编号必须沿用（中间压过一轮也不能换号）──
rows2 = conn.execute("SELECT name, inner_no FROM products ORDER BY id").fetchall()
chk("内部编号沿用", {r["name"]: r["inner_no"] for r in rows2}, inner_before)

# ── 5. 撤销单行也不影响别的行 ──
chk("撤销乙", reject_mod.unreject(DOC_NO, LATER, path=REJ), True)
chk("撤销后记录 0 条", len(reject_mod.load(path=REJ)["records"]), 0)

conn.close()
for f in (path, APV, REJ):
    if os.path.exists(f):
        os.remove(f)
try:
    os.remove(DB)
except OSError:
    pass
shutil.rmtree(OUT, ignore_errors=True)
print("ALL_OK", ok)
sys.exit(0 if ok else 1)
