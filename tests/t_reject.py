# -*- coding: utf-8 -*-
"""「禁止入库」全链回归。

跑法：python tmp/t_reject.py
覆盖：reject.py 增删去重 + run.py 把被禁行判成不可提交 + pipeline.step_for 给 all_rejected。
注：one_doc 已改名 step_for（2026-11-23 定位变更，不再有回填段），这里不再 mock 美亚。
"""
import sys, io, os, json
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
import reject as R, run, pipeline, emit, localdb

P = os.path.join(ROOT, "tmp", "rejects_test.json")
if os.path.exists(P):
    os.remove(P)
R.reject("DOC1", "物品A", reason="赠品", by="t", path=P)
R.reject("DOC1", "物品B", reason="样品", by="t", path=P)
print("HIT", R.is_rejected("DOC1", "物品A", path=P), "MISS", R.is_rejected("DOC1", "物品C", path=P))
print("COUNT", len(R.load(path=P)["records"]))
print("UNREJ", R.unreject("DOC1", "物品A", path=P), "LEFT", len(R.load(path=P)["records"]))
print("UNREJ_AGAIN", R.unreject("DOC1", "物品A", path=P))
R.reject("DOC1", "物品B", reason="样品2", by="t", path=P)
print("DEDUP", len(R.load(path=P)["records"]), R.load(path=P)["records"][0]["reason"])

ok = True
def chk(label, got, want):
    global ok
    good = got == want
    ok = ok and good
    print("%-26s %-16s %s" % (label, got, "OK" if good else "WANT " + str(want)))

# 端到端：run.py 把被禁行记进 rows_rejected，整单全禁时 step_for 给 all_rejected
#
# 夹具自造（早先这里读的是生产批次目录里的真实单据 + 一份真实产品库快照，
# 开源仓库里没有那些文件，也不该有）。这里只验「禁入行 → rows_rejected」，
# 用一张两行的合成单据就够：product_name 必须和下面 reject 打标记用的名字一致。
DOC_NO = "DEMOREJ0001"
ITEM_A = "示例 树脂水门汀"
ITEM_B = "示例 一次性手套"
src = os.path.join(TMP, DOC_NO + "_extracted.json")
io_ = __import__("io")
io_.open(src, "w", encoding="utf-8").write(__import__("json").dumps({
    "document": {"doc_no": DOC_NO, "doc_date": "2026-11-20",
                 "supplier_raw": "示例医疗器械有限公司"},
    "lines": [
        {"product_name": ITEM_A, "spec": "2支/盒", "brand": "示例牌",
         "license_no": "国械注准20203010001", "unit": "盒", "quantity": 2,
         "unit_price": 100.0, "amount": 200.0, "lot_no": "L1",
         "expiry_date": "2030-01-01", "manufacturer": "示例医疗器械有限公司"},
        {"product_name": ITEM_B, "spec": "100只/盒", "brand": "示例牌",
         "license_no": "鲁械注准20203010002", "unit": "盒", "quantity": 1,
         "unit_price": 30.0, "amount": 30.0, "lot_no": "L2",
         "expiry_date": "2030-06-01", "manufacturer": "示例医疗器械有限公司"},
    ], "warnings": []}, ensure_ascii=False))
# 本地库换成临时库并指过去：library 传空就走 emit.lib_from_db()，
# 不指开就会去读仓库里的 data/local.db（那里没有这两条物品 → 全判待建档）。
DB = os.path.join(TMP, "t_reject.db")
if os.path.exists(DB):
    os.remove(DB)
localdb.init(DB)
localdb.DB_PATH = DB
# 把 B 标记「禁止入库」：这样 rec 里只会剩 A 一行被拦下之外的情况可断言。
# 禁入记录也走临时文件——不指开就会写进仓库的 config/inbound_rejects.json，
# 跑一次测试就在别人仓库里留一条假记录。
_REJ = os.path.join(TMP, "t_reject_rejects.json")
if os.path.exists(_REJ):
    os.remove(_REJ)
R.REJECTS = _REJ
R.reject(DOC_NO, ITEM_B, reason="样品", by="t")
rec, _ = run.process_one(src, {"template_id": "x"}, os.path.join(ROOT, "out", "tmp_rej"), None, 99900, set())
print("rec rows", rec.get("rows"), "rows_rejected", len(rec.get("rows_rejected") or []))

rec2 = dict(rec)
rec2["rows_rejected"] = ["占位"] * int(rec.get("rows") or 1)
dec = {"doc_no": rec.get("doc_no") or "TESTREJ0001", "doc_date": rec.get("doc_date") or "",
       "state": emit.ST_EMPTY, "total": rec.get("rows") or 1}
em = {"out_dir": os.path.join(ROOT, "out", "tmp_rej"), "products_file": "", "inbound_file": ""}
step = pipeline.step_for(rec2, dec, em, use_ledger=False)
chk("ALL_REJECTED", step.get("state"), "all_rejected")
print("hint:", (step.get("detail") or {}).get("hint"))
print("ALL_OK", ok)
sys.exit(0 if ok else 1)
