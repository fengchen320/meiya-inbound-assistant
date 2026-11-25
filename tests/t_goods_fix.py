# -*- coding: utf-8 -*-
import sys, json, io
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
import lib_yae

# 真实订单对象（goodsList 已缩到需要的字段）
meta = {"orderNo": "MS260127163114015050", "createTime": 1769502674, "orderStatus": 1,
        "goodsList": [{"goodsName": "华柚 一次性使用无菌手术组件/手术包/种植包（低配款）",
                       "skuName": "II型 低配款", "goodsNum": 2, "shipQty": 2, "sendNum": 2,
                       "salePrice": 2990, "paidPrice": 2990, "goodsMoney": 5980,
                       "orderMoney": 5980, "payMoney": 5980, "adjustMoney": 5980,
                       "goodsLot": "7125122410", "expiryDate": "2027-12-23",
                       "factory": "河南省超亚医疗器械有限公司"}]}
lines = [{"_row": 2, "product_name": "华柚 一次性使用无菌手术组件/手术包/种植包（低配款）",
          "unit": "包", "quantity": 0.0, "unit_price": 29.9, "amount": 0.0,
          "lot_no": "", "expiry_date": "1900-01-01", "mfg_date": "",
          "manufacturer": "河南省博越医疗器械有限公司"}]
out, notes = lib_yae.fix_with_goods_list(lines, meta)
ln = out[0]
print("QTY", ln["quantity"], "PRICE", ln["unit_price"], "AMT", ln["amount"])
print("LOT", ln["lot_no"], "EXP", ln["expiry_date"])
print("NOTES", len(notes))
for n in notes: print("  -", n)
ok = (abs(ln["quantity"] - 2) < 1e-9 and abs(ln["unit_price"] - 29.9) < 1e-6
      and abs(ln["amount"] - 59.8) < 1e-6 and ln["lot_no"] == "7125122410"
      and ln["expiry_date"] == "2027-12-23"
      and any("厂家冲突" in n for n in notes))
print("ASSERT", ok)

# 回归：一致时不得改任何东西、不得产生告警
l2 = [{"_row": 2, "product_name": "华柚 一次性使用无菌手术组件/手术包/种植包（低配款）",
       "quantity": 2.0, "unit_price": 29.9, "amount": 59.8, "lot_no": "7125122410",
       "expiry_date": "2027-12-23", "manufacturer": "河南省超亚医疗器械有限公司"}]
o2, n2 = lib_yae.fix_with_goods_list(l2, meta)
print("NOCHANGE", n2 == [], "QTY", o2[0]["quantity"], "AMT", o2[0]["amount"])

# 回归：行数与明细不齐时不得按顺序硬配
l3 = [{"_row": 2, "product_name": "完全不同的物品", "quantity": 0.0, "unit_price": 0.0, "amount": 0.0,
       "lot_no": "", "expiry_date": "", "manufacturer": ""},
      {"_row": 3, "product_name": "另一件", "quantity": 0.0, "unit_price": 0.0, "amount": 0.0,
       "lot_no": "", "expiry_date": "", "manufacturer": ""}]
o3, n3 = lib_yae.fix_with_goods_list(l3, meta)
print("NOMATCH_SAFE", o3[0]["quantity"] == 0.0, "notes", len(n3))

ok = (ok and o2[0]["quantity"] == 2.0 and abs(o2[0]["amount"] - 59.8) < 1e-6
      and o3[0]["quantity"] == 0.0 and len(n3) == 0)
print("ALL_OK", ok)
sys.exit(0 if ok else 1)
