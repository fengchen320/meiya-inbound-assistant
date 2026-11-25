# -*- coding: utf-8 -*-
"""台账/对账显示的三个口径回归（用户 2026-11-25 三条要求）。

跑法：python tmp/t_recon.py

要求原文（m13879）：
  1、台账改为按最近时间依次显示
  2、单据后面加入来源（平台或供货商）
  3、单据前方加入订单时间（梅苗苗按发货时间、牙e在线按订单号3-8位取值、
     照片或本地按制单时间或者开票时间算）

这里钉住的是「哪一天算这张单的订单时间」与「来源写什么」——
这两列一错，整屏台账就只是在好看，核对时对不上任何一张真单。
"""
import io, os, sys, time, tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import recon

ok = True


def chk(label, got, want):
    global ok
    good = got == want
    ok = ok and good
    print("%-46s %-24s %s" % (label, got, "OK" if good else "WANT " + str(want)))


# ---- 3、订单时间 ----
# 牙e在线：订单号第 3–8 位就是下单日期
chk("牙e SC260105134840015050", recon.order_date_of({"doc_no": "SC260105134840015050"}), "2026-01-05")
chk("牙e MS260912155757015050", recon.order_date_of({"doc_no": "MS260912155757015050"}), "2026-09-12")
chk("牙e 小写/带空格也认", recon.order_date_of({"doc_no": " sc260404192823015050 "}), "2026-04-04")
# 号码里的日期不成立（13 月）必须退回后面的口径，不能编一个日期出来
chk("牙e 假日期(13月)不乱编", recon.order_date_of({"doc_no": "SC261305134840015050"}), "")
# 梅苗苗：发货时间落在 documents.doc_date
chk("梅苗苗按发货时间", recon.order_date_of({"doc_no": "76JP1YG97B0A3B8"},
                                            {"doc_date": "2026-08-28"}), "2026-08-28")
# 单据日期带时间也要截成日期
chk("单据日期带时间", recon.order_date_of({"doc_no": "X1"}, {"doc_date": "2026-08-28 10:22:00"}), "2026-08-28")
# 照片/本地：没有可读日期就退回文件修改时间（空着比猜更糟）
fd, tmp = tempfile.mkstemp(suffix=".jpg")
os.close(fd)
os.utime(tmp, (time.mktime((2026, 3, 9, 9, 0, 0, 0, 0, -1)),) * 2)
chk("照片退回文件时间", recon.order_date_of({"doc_no": "PHOTO1", "source": tmp}), "2026-03-09")
os.remove(tmp)
chk("没有文件也没有日期", recon.order_date_of({"doc_no": "PHOTO2", "source": "E:/no/such.jpg"}), "")

# ---- 2、来源 ----
chk("牙e 按来源路径认", recon.origin_label({"source": os.path.join(ROOT, "out", "pull", "yae", "x_extracted.json")}), "牙e在线")
chk("梅苗苗 按来源路径认", recon.origin_label({"source": os.path.join(ROOT, "out", "pull", "mmm", "x_extracted.json")}), "梅苗苗")
chk("照片 按扩展名认", recon.origin_label({"source": os.path.join(ROOT, "tmp", "a", "b.jpg")}), "照片")
chk("认不出平台就写供货商", recon.origin_label(
    {"source": os.path.join(ROOT, "tmp", "x", "y_extracted.json"), "supplier": "示例医疗器械有限公司"}),
    "示例医疗器械有限公司")
chk("什么都没有写本地文件", recon.origin_label({"source": os.path.join(ROOT, "tmp", "x.json")}), "本地文件")
# 记录里已经带 origin（新批次）时以它为准；「历史台账」是灌库时的占位标签，不能当来源用
chk("已有 origin 优先", recon.origin_label({"origin": "梅苗苗", "source": "E:/x/y.jpg"}), "梅苗苗")
chk("历史台账占位不算来源", recon.origin_label(
    {"origin": "历史台账", "source": os.path.join(ROOT, "out", "pull", "yae", "z.json")}), "牙e在线")

# ---- 1、按最近时间倒序 ----
recs = [{"doc_no": "A", "at": "2026-11-22 10:00:00"},
        {"doc_no": "B", "at": "2026-11-25 09:00:00"},
        {"doc_no": "C", "at": "2026-11-23 18:00:00"}]
chk("最近时间在最前", [r["doc_no"] for r in recon.recent(recs)], ["B", "C", "A"])
chk("空输入不炸", recon.recent(None), [])

# 第一列是订单时间，这一列就必须单调：只按记账时间排会让 7 月的单排到 9 月前面
mixed = [{"doc_no": "JUL", "order_date": "2026-07-01", "at": "2026-11-25 12:00:00"},
         {"doc_no": "SEP", "order_date": "2026-09-20", "at": "2026-11-25 11:00:00"},
         {"doc_no": "NONE", "order_date": "", "at": "2026-11-25 13:00:00"}]
chk("按订单时间倒序", [r["doc_no"] for r in recon.by_order(mixed)], ["SEP", "JUL", "NONE"])
chk("没订单时间的排最后", [r["doc_no"] for r in recon.by_order(mixed)][-1], "NONE")
chk("by_order 空输入不炸", recon.by_order(None), [])

# ---- decorate：从 documents 借 origin / doc_date ----
docs = [{"doc_no": "SC260105134840015050", "origin": "牙e在线", "doc_date": "2026-01-05"}]
out = recon.decorate([{"doc_no": " SC260105134840015050 ", "at": "2026-01-06 10:00:00",
                       "source": os.path.join(ROOT, "out", "pull", "yae", "x.json")}], docs, db={})
# 不在本批里的老台账单，要能靠本地库 documents 补上（否则订单时间整片是「—」）
out2 = recon.decorate([{"doc_no": "76JP1YG97B0A3B8", "at": "2026-11-25 11:57:28",
                        "source": os.path.join(ROOT, "out", "pull", "mmm", "x.json")}], [],
                      db={"76JP1YG97B0A3B8": {"doc_no": "76JP1YG97B0A3B8", "origin": "梅苗苗",
                                              "doc_date": "2026-08-28"}})
chk("老单靠本地库补订单时间", out2[0]["order_date"], "2026-08-28")
chk("老单靠本地库补来源", out2[0]["origin_label"], "梅苗苗")
chk("decorate 带来源", out[0]["origin_label"], "牙e在线")
chk("decorate 带订单时间", out[0]["order_date"], "2026-01-06"[0:0] + "2026-01-05")
b = recon.brief(out[0])
# confirmed / confirmed_at 是 2026-11-25 加的：出过表不等于进了库存，
# 台账那一屏要能显示「已入库 / 未确认」，所以 brief 必须带出来。
chk("brief 只给界面要的字段", sorted(b.keys()),
    sorted(["doc_no", "lines", "amount", "at", "origin", "order_date", "supplier",
            "note", "confirmed", "confirmed_at"]))
chk("brief 不带本机路径", "source" in b, False)

print("ALL_OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
