# -*- coding: utf-8 -*-
"""emit 的判定单测：need_build / pending 仍在建档表 / awaiting / rejected。"""
import io, json, os, sys, tempfile
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import localdb, emit, build_import

DB = os.path.join(os.path.join(ROOT, "tmp"), "t_emit.db")
if os.path.exists(DB):
    os.remove(DB)
localdb.init(DB)
conn = localdb.connect(DB)

DOC = {
    "document": {"doc_no": "TESTEMIT0001", "doc_date": "2026-02-01",
                 "supplier_raw": "测试供应商"},
    "lines": [
        {"product_name": "测试 未知耗材甲", "spec": "100支/袋", "brand": "测试牌",
         "license_no": "川械注准20250001", "unit": "袋", "quantity": 2,
         "unit_price": 10.0, "amount": 20.0, "lot_no": "L001",
         "expiry_date": "2028-01-01", "manufacturer": "测试厂"},
        {"product_name": "测试 未知耗材乙", "spec": "50支/盒", "brand": "测试牌",
         "license_no": "川械注准20250002", "unit": "盒", "quantity": 1,
         "unit_price": 20.0, "amount": 20.0, "lot_no": "L002",
         "expiry_date": "2028-01-01", "manufacturer": "测试厂"},
    ],
}
p = os.path.join(os.path.join(ROOT, "tmp"), "TESTEMIT0001_extracted.json")
json.dump(DOC, open(p, "w", encoding="utf-8"), ensure_ascii=False)

r = emit.emit([p], os.path.join(os.path.join(ROOT, "out"), "emit", "t_emit"), conn=conn)
d = r["docs"][0]
print("1 state:", d["state"], "| inbound", len(d["inbound_rows"]), "| need_build", len(d["need_build"]))
assert d["state"] == "need_build", d["state"]
assert len(d["need_build"]) == 2
print("1 OK products:", r["counts"]["products"])
assert r["counts"]["products"] == 2

# 第二次：把其中一个建成 built（给物品编号），另一个保持 pending
conn.execute("UPDATE products SET item_no='10999', status='built' WHERE name LIKE '%甲%'")
conn.commit()
r2 = emit.emit([p], os.path.join(os.path.join(ROOT, "out"), "emit", "t_emit2"), conn=conn)
d2 = r2["docs"][0]
print("2 state:", d2["state"], "| inbound", len(d2["inbound_rows"]), "| need_build", len(d2["need_build"]))
assert d2["state"] == "need_build"
assert len(d2["inbound_rows"]) == 1, len(d2["inbound_rows"])
# pending 的乙必须仍在建档表里（它还没导进美亚）
names = [x["物品名称"] for x in d2["need_build"]]
print("2 need_build names:", names)
assert any("乙" in x for x in names), names
print("2 OK")

# 第三次：两个都 built
conn.execute("UPDATE products SET item_no='10998', status='built' WHERE name LIKE '%乙%'")
conn.commit()
r3 = emit.emit([p], os.path.join(os.path.join(ROOT, "out"), "emit", "t_emit3"), conn=conn)
print("3 state:", r3["docs"][0]["state"], "| inbound", len(r3["docs"][0]["inbound_rows"]))
assert r3["docs"][0]["state"] == "ready"
assert r3["counts"]["products"] == 0
print("3 OK")

conn.close()
print("ALL_OK")
