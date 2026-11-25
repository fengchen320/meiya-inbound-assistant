# -*- coding: utf-8 -*-
"""确认入库 + 台账排序 / 来源过滤的口径。

三件事必须钉住：
  1. ledger.confirm 是「就地打标记」而不是补一条记录 —— 补记录会让 check() 读到两条
     同号记录，first_at 和金额会读错，判重也可能误判。
  2. 老台账（没有 confirmed_at）一律当「未确认」：那时程序会自己点直接入库，但
     note 写的是「已出表」还是老文案并不可靠，替人担保库存比多问一次危险得多。
  3. 排序键：默认记账时间倒序；切到订单时间时，没有订单时间的行必须排最后，
     不能拿记账日期顶上（实测会把 8 月发货的单显示成 11-22，像刚下的单）。
"""
import io, json, os, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

import ledger
import recon

OK = []
BAD = []


def ck(cond, msg):
    (OK if cond else BAD).append(msg)
    print(("  ok  " if cond else "  FAIL ") + msg)


TMP = os.path.join(HERE, "t_confirm_ledger.json")
shutil.rmtree(os.path.join(ROOT, "out", "emit", "t_confirm"), ignore_errors=True)
try:
    os.remove(TMP)
except OSError:
    pass

print("[1] 确认入库：只打标记，不新增记录")
ledger.record("SCTEST001", [{"product_name": "测试 甲", "quantity": 1, "amount": 10}], path=TMP)
n0 = len(ledger.load(TMP)["records"])
rec, first = ledger.confirm("SCTEST001", note="走界面", path=TMP)
ck(n0 == 1 and len(ledger.load(TMP)["records"]) == 1, "确认后台账仍是 1 条（没补新记录）")
ck(first is True and bool(rec.get("confirmed_at")), "第一次确认 first=True 且写了 confirmed_at")
ck(rec.get("confirmed_by") == "ui", "confirmed_by=ui")
ck(ledger.is_confirmed("SCTEST001", path=TMP) is True, "is_confirmed 为 True")
ck(ledger.check("SCTEST001", [{**{"product_name": "测试 甲", "quantity": 1, "amount": 10}}],
                path=TMP)[0] == "dup", "确认之后判重仍是 dup（没影响指纹）")

print("[2] 再点一次：幂等")
rec2, first2 = ledger.confirm("SCTEST001", path=TMP)
ck(first2 is False, "第二次 first=False")
ck(rec2.get("confirmed_at") == rec.get("confirmed_at"), "确认时间不被刷新（保留第一次的时间）")
ck(len(ledger.load(TMP)["records"]) == 1, "仍然只有 1 条记录")

print("[3] 撤销")
ledger.unconfirm("SCTEST001", path=TMP)
ck(ledger.is_confirmed("SCTEST001", path=TMP) is False, "撤销后 is_confirmed False")
r = ledger.load(TMP)["records"][0]
ck("confirmed_by" not in r and "confirm_note" not in r, "撤销把 confirmed_by / confirm_note 也清掉")
ck(ledger.unconfirm("SCTEST001", path=TMP) is None, "本来没确认时再撤销返回 None")

print("[4] 老记录（没有 confirmed_at）一律当未确认")
with open(TMP, encoding="utf-8") as f:
    j = json.load(f)
j["records"].append({"doc_no": "SCTEST002", "fingerprint": "x", "at": "2026-01-01 00:00:00",
                     "lines": 1, "amount": 1, "note": "已出表"})
with open(TMP, "w", encoding="utf-8") as f:
    json.dump(j, f, ensure_ascii=False)
ck(ledger.is_confirmed("SCTEST002", path=TMP) is False, "老记录 is_confirmed False")
ck(ledger.confirm("NOSUCH", path=TMP)[0] is None, "台账里没有的单返回 None")
try:
    ledger.confirm("", path=TMP)
    ck(False, "空单号必须抛错")
except ValueError:
    ck(True, "空单号抛 ValueError")

print("[5] 来源三档")
ck(recon.origin_key("梅苗苗") == "梅苗苗", "梅苗苗")
ck(recon.origin_key("牙e在线") == "牙e在线", "牙e在线")
ck(recon.origin_key("牙易在线") == "牙e在线", "牙易在线 也归牙e在线")
ck(recon.origin_key("照片") == "其他", "照片 归其他")
ck(recon.origin_key("示例医疗器械有限公司") == "其他", "认不出平台退成供货商名 → 其他")
ck(recon.origin_key("") == "其他", "空值 → 其他")
ck(recon.origin_key(None) == "其他", "None → 其他")
ck(list(recon.ORIGIN_KEYS) == ["梅苗苗", "牙e在线", "其他"], "档位顺序固定（界面按这个顺序出按钮）")

print("[6] 排序")
rows = [
    {"doc_no": "A", "at": "2026-11-22 10:00:00", "order_date": "2026-11-20"},
    {"doc_no": "B", "at": "2026-11-25 09:00:00", "order_date": "2026-08-01"},
    {"doc_no": "C", "at": "2026-11-23 12:00:00", "order_date": ""},
    {"doc_no": "D", "at": "2026-11-24 08:00:00", "order_date": "2026-11-24"},
]
ck([x["doc_no"] for x in recon.by_moment(list(rows), "at")] == ["B", "D", "C", "A"],
   "默认记账时间倒序")
od = [x["doc_no"] for x in recon.by_moment(list(rows), "order")]
ck(od == ["D", "A", "B", "C"], "订单时间倒序，无订单时间的 C 排最后")
ck(od[-1] == "C", "C 没订单时间但没有被记账日期顶上来（否则会显示成 11-23 像最新）")
ck([x["doc_no"] for x in recon.by_moment(list(rows), "nonsense")] == ["B", "D", "C", "A"],
   "非法 kind 回落记账时间")

print("[7] brief 带 confirmed")
b = recon.brief({"doc_no": "X", "lines": 2, "amount": 3, "at": "2026-11-25",
                 "confirmed_at": "2026-11-25 13:00:00"})
ck(b["confirmed"] is True and b["confirmed_at"].startswith("2026-11-25"), "已确认的 brief")
b2 = recon.brief({"doc_no": "Y", "lines": 1, "amount": 1, "at": "2026-11-25"})
ck(b2["confirmed"] is False and b2["confirmed_at"] == "", "未确认的 brief")

print("[8] backfill_confirmed：老台账一次性标已入库（用户 2026-11-25 第 2 条）")
# 挪一份干净的台账来测：旧记录一条（11-20）、当天稍晚一条（11-25 10:00）、已确认一条。
BK = os.path.join(HERE, "t_confirm_backfill.json")
with open(BK, "w", encoding="utf-8") as f:
    json.dump({"version": 1, "records": [
        {"doc_no": "OLD1", "fingerprint": "a", "at": "2026-11-20 09:00:00", "lines": 1, "amount": 5},
        {"doc_no": "OLD2", "fingerprint": "b", "at": "2026-11-23 18:30:00", "lines": 2, "amount": 9},
        {"doc_no": "NEW1", "fingerprint": "c", "at": "2026-11-25 10:00:00", "lines": 1, "amount": 3},
        {"doc_no": "DONE", "fingerprint": "d", "at": "2026-11-21 08:00:00", "lines": 1, "amount": 1,
         "confirmed_at": "2026-11-22 08:00:00", "confirmed_by": "ui"},
    ]}, f, ensure_ascii=False)
n = ledger.backfill_confirmed(only_before="2026-11-25 00:00:00", path=BK)
recs = {r["doc_no"]: r for r in ledger.load(BK)["records"]}
ck(n == 2, "只补了 2026-11-25 之前的两条（%d）" % n)
ck(recs["NEW1"].get("confirmed_at") is None, "当天 10:00 那条不动（那是新流程，要人真点）")
# 关键：补录时间用记录自己的时间，不是「现在」。写成今天，对账时会对不上那天入的库。
ck(recs["OLD1"]["confirmed_at"] == "2026-11-20 09:00:00", "confirmed_at 用记录自己的 at")
ck(recs["OLD2"]["confirmed_at"] == "2026-11-23 18:30:00", "第二条同上")
ck(recs["OLD1"]["confirmed_by"] == "migrate", "confirmed_by=migrate（不是 ui，看得出是迁移）")
ck(bool(recs["OLD1"].get("confirm_note")), "confirm_note 写了迁移说明")
ck(recs["DONE"]["confirmed_at"] == "2026-11-22 08:00:00", "本来就是已确认的，时间不被覆盖")
ck(recs["DONE"]["confirmed_by"] == "ui", "本来就是已确认的，来源也不被覆盖")
ck(ledger.backfill_confirmed(only_before="2026-11-25 00:00:00", path=BK) == 0, "再跑一次补 0 条（幂等）")
ck(ledger.is_confirmed("OLD1", path=BK) is True, "补完之后 is_confirmed True")
try:
    os.remove(BK)
except OSError:
    pass

try:
    os.remove(TMP)
except OSError:
    pass
shutil.rmtree(os.path.join(ROOT, "out", "emit", "t_confirm"), ignore_errors=True)

print("")
print("OK %d / FAIL %d" % (len(OK), len(BAD)))
print("ALL_OK", not BAD)
sys.exit(0 if not BAD else 1)
