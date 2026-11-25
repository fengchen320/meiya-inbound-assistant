# -*- coding: utf-8 -*-
"""整体合并出表回归：入库表按供应商分组（2026-11-24，用户 m11115）。

跑法：python tmp/t_group.py
断言三件事：
  1. 同一批两家供应商 → 两张入库表，行数各归各的，没有混行；
  2. 同一家供应商的多张单 → 合成一张（不是一单一张，那正是用户抱怨的东西）；
  3. 只有一家供应商时文件名仍是 inbound_all.xlsx，且 inbound_file 指向它
     （老批次、老脚本与对账都按这个字段找表，改名等于把已有的东西弄坏）。
"""
import io, json, os, shutil, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import localdb, emit, openpyxl

OUT = os.path.join(ROOT, "out", "emit", "t_group")
if os.path.isdir(OUT):
    shutil.rmtree(OUT)

DB = os.path.join(ROOT, "tmp", "t_group.db")
if os.path.exists(DB):
    os.remove(DB)
localdb.init(DB)
conn = localdb.connect(DB)


def doc(no, sup, date, name, license_no):
    return {"document": {"doc_no": no, "doc_date": date, "supplier_raw": sup},
            "lines": [{"product_name": name, "spec": "1支/盒", "brand": "测试牌",
                       "license_no": license_no, "unit": "盒", "quantity": 1,
                       "unit_price": 10.0, "amount": 10.0, "lot_no": "L1",
                       "expiry_date": "2028-01-01", "manufacturer": "测试厂"}]}


def save(no, **kw):
    p = os.path.join(os.path.join(ROOT, "tmp"), no + "_extracted.json")
    json.dump(doc(no, **kw), open(p, "w", encoding="utf-8"), ensure_ascii=False)
    return p


ok = True


def chk(label, got, want):
    global ok
    good = got == want
    ok = ok and good
    print("%-32s %-26s %s" % (label, got, "OK" if good else "WANT " + str(want)))


A = "四川牙易在线网络科技有限公司"
B = "示例医药有限公司"
pa = save("TESTGRPA0001", sup=A, date="2026-03-01", name="测试 分组耗材A", license_no="川械注准20259001")
pc = save("TESTGRPA0002", sup=A, date="2026-03-03", name="测试 分组耗材C", license_no="川械注准20259003")
pb = save("TESTGRPB0001", sup=B, date="2026-03-02", name="测试 分组耗材B", license_no="川械注准20259002")

# 先出一次表把三件物品记进本地库（pending），再给物品编号。
# 不给物品编号时 by_ident 里没有它们，每行都判「待建档」→ 入库表 0 行（第一次跑就踩了）。
emit.emit([pa, pc, pb], os.path.join(OUT, "warm"), conn=conn)
for nm, item in (("测试 分组耗材A", "10801"), ("测试 分组耗材B", "10802"),
                 ("测试 分组耗材C", "10803")):
    n = conn.execute("UPDATE products SET item_no=?, status='built' WHERE name=?",
                     (item, nm)).rowcount
    chk("标记已建档 " + nm[-1], n, 1)
conn.commit()

# ── 1. 两家供应商 ────────────────────────────────────────────────
r = emit.emit([pa, pb], os.path.join(OUT, "two"), conn=conn)
g = r["inbound_groups"]
chk("两家 -> 表数", len(g), 2)
chk("两家 -> inbound_file 置空", r["inbound_file"], "")
chk("行数合计", sum(x["rows"] for x in g), 2)
chk("分组名对上", sorted(x["supplier"] for x in g), sorted([A, B]))
for x in g:
    chk("文件存在 " + os.path.basename(x["file"])[:16], os.path.exists(x["file"]), True)
    wb = openpyxl.load_workbook(x["file"])
    chk("行数(表内) " + os.path.basename(x["file"])[:16], wb.active.max_row - 2, 1)

wa = openpyxl.load_workbook([x for x in g if x["supplier"] == A][0]["file"])
names_a = [wa.active.cell(row=i, column=1).value for i in range(3, wa.active.max_row + 1)]
chk("A 表里没有 B 的货", any("耗材B" in str(n) for n in names_a), False)

# ── 2. 同一家两张单合成一张 ──────────────────────────────────────
r2 = emit.emit([pa, pc], os.path.join(OUT, "same"), conn=conn)
chk("同家两单 -> 表数", len(r2["inbound_groups"]), 1)
chk("同家两单 -> 行数", r2["inbound_groups"][0]["rows"], 2)
chk("同家两单 -> 文件名", os.path.basename(r2["inbound_file"]), "inbound_all.xlsx")

dec = json.load(open(os.path.join(OUT, "same", "decision.json"), encoding="utf-8"))
chk("decision 带 groups", len(dec.get("inbound_groups") or []), 1)

# ── 3. 上一轮的旧入库表必须被清掉 ────────────────────────────────
# 复用同一个 out 目录时留着旧表，人下次会照着旧表导一遍（库存翻倍）。
stale = os.path.join(OUT, "same", "inbound_旧供应商.xlsx")
open(stale, "wb").write(b"x")
r3 = emit.emit([pa], os.path.join(OUT, "same"), conn=conn)
chk("旧入库表被清掉", os.path.exists(stale), False)
chk("重出后仍一张表", len(r3["inbound_groups"]), 1)

print("ALL_OK", ok)
sys.exit(0 if ok else 1)
