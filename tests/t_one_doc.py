# -*- coding: utf-8 -*-
"""pipeline.step_for 状态机回归（one_doc 已在定位变更后改名 step_for）。

跑法：python tmp/t_one_doc.py
断言重点：判重仍然生效（dup / force 放行），且新状态都出得来。
"""
import io, sys, os, json, tempfile
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import ledger, pipeline, emit

# 夹具自造：这个用例只验 step_for 的状态机，不需要真实单据。
DOC_NO = "DEMO0001"
ext = os.path.join(TMP, DOC_NO + "_extracted.json")
lines = [
    {"product_name": "示例 树脂水门汀", "spec": "2支/盒", "brand": "示例牌",
     "license_no": "国械注准20203010001", "unit": "盒", "quantity": 2,
     "unit_price": 100.0, "amount": 200.0, "lot_no": "L1",
     "expiry_date": "2030-01-01", "manufacturer": "示例医疗器械有限公司"},
    {"product_name": "示例 一次性手套", "spec": "100只/盒", "brand": "示例牌",
     "license_no": "鲁械注准20203010002", "unit": "盒", "quantity": 1,
     "unit_price": 30.0, "amount": 30.0, "lot_no": "L2",
     "expiry_date": "2030-06-01", "manufacturer": "示例医疗器械有限公司"},
]
json.dump({"document": {"doc_no": DOC_NO, "doc_date": "2026-11-20",
                        "supplier_raw": "示例医疗器械有限公司"},
           "lines": lines, "warnings": []},
          io.open(ext, "w", encoding="utf-8"), ensure_ascii=False)
doc_no = DOC_NO
print("ext", doc_no, "lines", len(lines))

tmp = os.path.join(tempfile.gettempdir(), "ledger_step_for.json")
if os.path.exists(tmp):
    os.remove(tmp)
ledger.LEDGER = tmp          # 临时台账，别碰真文件

rec = {"source": "x.jpg", "stem": DOC_NO, "doc_no": doc_no,
       "origin": "照片", "extracted": ext, "rows": len(lines), "line_names": [l.get("product_name") for l in lines]}
dec = {"doc_no": doc_no, "doc_date": "2026-11-20", "origin": "照片",
       "state": emit.ST_READY, "inbound_rows": [lines[0]], "total": len(lines),
       "line_names": rec["line_names"], "supplier": "示例医疗器械有限公司"}
em = {"out_dir": os.path.join(ROOT, "out", "emit", "test"), "products_file": "", "inbound_file": os.path.join(ROOT, "out", "emit", "test", "inbound_all.xlsx")}

ok = True
def chk(label, got, want):
    global ok
    good = got == want
    ok = ok and good
    print("%-28s %-18s %s" % (label, got, "OK" if good else "WANT " + str(want)))

print("1 check new ->", ledger.check(doc_no, lines)[0])
chk("2 emit 前 state", pipeline.step_for(rec, dec, em)["state"], "emitted")

ledger.record(doc_no, lines, {"supplier": "示例医疗器械有限公司", "note": "已出表"})
chk("3 已记台账 -> dup", pipeline.step_for(rec, dec, em)["state"], "duplicate")
chk("4 force 放行", pipeline.step_for(rec, dec, em, force=True)["state"], "emitted")

rec_noid = dict(rec, doc_no="")
dec_noid = dict(dec, doc_no="")
r = pipeline.step_for(rec_noid, dec_noid, em)
chk("5 无单号不出 dup", r["state"], "emitted")
chk("5b 无单号带 warn", bool((r.get("detail") or {}).get("warn_docno")), True)

r = pipeline.step_for(dict(rec, all_gift_skipped=True, rows_skipped_gift=["香蕉"]), dec, em)
chk("6 全赠品", r["state"], "skipped_gift")

r = pipeline.step_for(dict(rec, rows_rejected=["X"], rows=1), dec, em)
chk("7 全禁入", r["state"], "all_rejected")

# 8/9 用新单号：judge 顺序是「先判重后看状态」，拿已经记过台账的单号会先撞 duplicate。
# 真实流程里 need_build / awaiting 的单不会记账（main 只在 emitted 时记），所以不会误伤。
rec_fresh = dict(rec, doc_no="TESTFRESH0001")
dec_fresh = dict(dec, doc_no="TESTFRESH0001")
dec_nb = dict(dec_fresh, state=emit.ST_NEED_BUILD, need_build=[{"物品名称": "X"}])
chk("8 待建档", pipeline.step_for(rec_fresh, dec_nb, em)["state"], "need_build")
# 单号以 dec（emit 的结论）优先：rec 里那份是 run.py 读出来的，emit 里那份才带规范化
chk("8b dec 单号优先", pipeline.step_for(rec, dec_fresh, em)["doc_no"], "TESTFRESH0001")

dec_ap = dict(dec_fresh, state=emit.ST_AWAITING, approval=[{"物品名称": "X", "有效期": ""}])
chk("9 待审批", pipeline.step_for(rec_fresh, dec_ap, em)["state"], "awaiting_approval")

print("ALL_OK", ok)
sys.exit(0 if ok else 1)
