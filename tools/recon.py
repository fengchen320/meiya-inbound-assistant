# -*- coding: utf-8 -*-
"""recon.py —— 台账 ↔ 出表产物 对账（本地，不连任何外部系统）。

为什么还要对账：2026-11-23 起本程序不再操作美亚，只负责出两张表。于是「错账」
的形态变了 —— 不再是「点了直接入库却没进库存」，而是：

    台账说「这张单已出过表」→ 下次重跑会被判 duplicate 跳过
    但那张表其实没出出来 / 出到一半失败 / 被后一批覆盖

所以要能在出完表之后核一遍：台账里记的每一单，是不是真的在最近一次出表的
decision.json 里、行数对不对得上。对不上就是台账记早了（或表丢了），必须查。

用法：
    python tools/recon.py                 # 核最近一个出表批次
    python tools/recon.py --dir out/emit/20261123_101500
    python tools/recon.py --ledger-only    # 只打台账，不核表

匹配口径（顺序）：
    1. 单据号精确命中
    2. 否则按「金额 ±0.02 且 行数相同」配对；有多个候选就取第一个
配不上的两边都单独列出来 —— 那才是要人看的。
"""
import argparse
import datetime
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import ledger            # noqa: E402

EMIT_ROOT = os.path.join(ROOT, "out", "emit")
OUT_ROOT = os.path.join(ROOT, "out")


def latest_dir(root=OUT_ROOT):
    """最近一个含 decision.json 的目录。emit 批次在 out/emit/<时间戳>，
    命令行跑 pipeline 时会把 decision.json 写进 --out 目录，所以两处都扫。"""
    best, best_t = None, -1.0
    for base in (EMIT_ROOT, root):
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for nm in names:
            d = os.path.join(base, nm)
            f = os.path.join(d, "decision.json")
            if not os.path.isfile(f):
                continue
            t = os.path.getmtime(f)
            if t > best_t:
                best, best_t = d, t
    return best


def load_decision(d):
    """读出表批次的 decision.json，返回 (documents, counts, files)。"""
    if not d:
        return [], {}, {}
    f = os.path.join(d, "decision.json")
    if not os.path.exists(f):
        return [], {}, {}
    with open(f, encoding="utf-8") as fh:
        j = json.load(fh)
    return (j.get("documents") or []), (j.get("counts") or {}), (j.get("files") or {})


# 牙e在线的订单号里就带着下单日期：SC260105134840015050 -> 260105 -> 2026-01-05。
# 用户 2026-11-25 定的口径是「按订单号第 3–8 位取值」，正好就是这个。
_YAE_DOCNO = re.compile(r"^(?:SC|MS)(\d{2})(\d{2})(\d{2})")


def _norm_no(s):
    return re.sub(r"\s+", "", str(s or "")).upper()


def _mtime_date(path):
    """文件修改时间当日期用。照片/本地单据常常没有可读的制单时间，
    抓不到就退回文件时间 —— 空着比猜一个更糟（人没法判断这行是新的还是旧的）。"""
    p = str(path or "")
    if not p or not os.path.isfile(p):
        return ""
    try:
        return datetime.date.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d")
    except (OSError, ValueError):
        return ""


def _ymd(y, m, d):
    try:
        datetime.date(int(y), int(m), int(d))
    except (TypeError, ValueError):
        return ""
    return "%04d-%02d-%02d" % (int(y), int(m), int(d))


def order_date_of(rec, doc=None):
    """这张单本身是哪天开的（订单时间）。

    三条口径（用户 2026-11-25 定）：
      牙e在线    订单号第 3–8 位（SC260105134840015050 -> 2026-01-05）
      梅苗苗      发货时间（抽取结果的 shipedDate，落在 documents.doc_date）
      照片/本地    单据上的制单/开票时间，取不到就用文件修改时间

    为什么不统一用「记账时间」：记账时间是出表那一刻，跟货哪天到的一点关系都没有。
    台账按它排看着整齐，实际上把半年的单据混成一锅，人核对时对不上任何一张单。
    """
    no = _norm_no(rec.get("doc_no"))
    m = _YAE_DOCNO.match(no)
    if m:
        dd = _ymd("20" + m.group(1), m.group(2), m.group(3))
        if dd:
            return dd
    cand = str(rec.get("doc_date") or (doc or {}).get("doc_date") or "").strip()
    # 单据日期可能带时间（2026-01-05 10:22:00），只取日期部分
    if len(cand) >= 10 and cand[4] == "-" and cand[7] == "-":
        try:
            datetime.date(int(cand[0:4]), int(cand[5:7]), int(cand[8:10]))
            return cand[:10]
        except ValueError:
            pass
    return _mtime_date(rec.get("source") or "")


def origin_label(rec, doc=None):
    """来源：能认出平台就写平台，认不出就写供货商。

    为什么退回供货商而不是写「本地文件」：人看到「本地文件」等于什么也没说，
    而供货商名字能直接回答「这批货是谁的」。平台这一栏是为了回答「这张单怎么来的」，
    两个问题落在同一列时，有信息量的那个优先。
    """
    o = str(rec.get("origin") or (doc or {}).get("origin") or "").strip()
    if o and o != "历史台账":
        return o
    src = str(rec.get("source") or "").replace("\\", "/").lower()
    if "/pull/yae/" in src:
        return "牙e在线"
    if "/pull/mmm/" in src:
        return "梅苗苗"
    if re.search(r"\.(jpg|jpeg|png|webp|bmp|tif|tiff)$", src):
        return "照片"
    sup = str(rec.get("supplier") or (doc or {}).get("supplier") or "").strip()
    return sup or "本地文件"


def db_index(conn=None):
    """documents 表按单据号索引：{单号: {doc_no, origin, doc_date, supplier}}。

    为什么不能只靠本批 decision.json：台账是流水账，里面绝大多数单不在「最近这一批」里，
    拿本批的 documents 去补它们永远补不上，订单时间一列会整片是「—」。
    本地库 documents 表从 2026-11-23 起每单都留 origin / doc_date，才是全量来源。
    """
    own = conn is None
    try:
        import localdb
        conn = conn or localdb.connect()
    except Exception:
        return {}
    try:
        rows = conn.execute(
            "SELECT doc_no, origin, doc_date, supplier FROM documents").fetchall()
        out = {}
        for r in rows:
            out[_norm_no(r["doc_no"])] = {
                "doc_no": r["doc_no"], "origin": r["origin"] or "",
                "doc_date": r["doc_date"] or "", "supplier": r["supplier"] or ""}
        return out
    except Exception:
        return {}
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def decorate(records, docs, db=None):
    """给台账记录补上「来源」和「订单时间」，附带把 documents 里的同一单对上。

    三处信息按优先级取：本批 decision.json（新批次）→ 本地库 documents 表（全量）
    → 台账自己的来源路径与文件时间（最后的兜底）。
    老台账记录只写了 source / supplier，「订单时间」「来源」两列全靠前两处补。
    """
    idx = {}
    for d in docs or []:
        idx.setdefault(_norm_no(d.get("doc_no")), d)
    if db is None:
        db = db_index()
    out = []
    for r in records or []:
        k = _norm_no(r.get("doc_no"))
        d = idx.get(k) or db.get(k) or {}
        q = dict(r)
        q["origin_label"] = origin_label(r, d)
        q["order_date"] = order_date_of(r, d)
        out.append(q)
    return out


def recent(items, key="at"):
    """最近的排前面。YYYY-MM-DD HH:MM:SS 的字符串序就是时间序，不用解析。"""
    return sorted(items or [], key=lambda x: str(x.get(key) or ""), reverse=True)


def by_order(items):
    """按「订单时间」倒序，没有订单时间的退回记账时间排。

    第一列既然是订单时间，这一列就得是单调的 —— 按记账时间排会让第一列忽前忽后
    （同一天出的表里混着 7 月的单和 9 月的单），人一眼看上去像没排序。
    没有订单时间的行（照片抓不到日期）按记账时间参与排序，不会全被挤到最后。
    """
    def key(x):
        od = str(x.get("order_date") or "")
        at = str(x.get("at") or "")
        # 有订单时间的排前面（按订单时间倒序）；没有的按记账时间排在后面。
        # 不能拿记账时间顶上去排：那张 11-22 才出表的单会显示在「2026-11-22」这一档，
        # 看上去像最新下的单，而它其实是 8 月发的货、日期根本没抓到。
        return (1 if od else 0, od, at)
    return sorted(items or [], key=key, reverse=True)


def by_moment(items, kind="at"):
    """按时间倒序。kind="at" 用记账时间（默认，用户 2026-11-25 定的口径），
    kind="order" 用订单时间。

    为什么默认改成记账时间：台账首先是「我们哪天出的表」这本流水，出完表当天
    就要核对「这几单导进美亚了没有」。按订单时间排会把三个月前下的单顶到最上面，
    而它们恰恰是最不需要现在看的一批。订单时间这一列仍然是给「这是哪天的单」
    用的，想看就切一下，别把两件事混成一个默认值。
    """
    if kind == "order":
        return by_order(items)
    return sorted(items or [], key=lambda x: str(x.get("at") or ""), reverse=True)


def brief(r):
    """给界面用的一份精简记录（路径不给前端，没必要）。

    confirmed / confirmed_at 是「人在美亚导完入库单、回界面点了确认」的标记。
    没有它，一行台账只说明「表出过了」，不说明「货进了库存」——
    这两件事以前是一回事（程序会点直接入库），2026-11-25 起必须分开。
    """
    return {
        "doc_no": r.get("doc_no"),
        "lines": r.get("lines"),
        "amount": r.get("amount"),
        "at": r.get("at"),
        "origin": r.get("origin_label") or "",
        "order_date": r.get("order_date") or "",
        "supplier": r.get("supplier") or "",
        "note": r.get("note") or "",
        "confirmed": bool(r.get("confirmed_at")),
        "confirmed_at": r.get("confirmed_at") or "",
    }


def match(records, docs):
    """返回 (pairs, only_ledger, only_docs)。pairs 是 [(rec, doc, how)]。

    how 说明是怎么配上的：单号 / 金额。人对不上时第一句话就是「它凭什么说这是同一单」。
    """
    used = set()
    pairs, only_ledger = [], []
    # 第一轮：单据号
    by_no = {}
    for d in docs:
        by_no.setdefault(str(d.get("doc_no") or ""), []).append(d)
    for r in records:
        no = str(r.get("doc_no") or "")
        cands = [d for d in by_no.get(no, []) if id(d) not in used]
        if cands:
            used.add(id(cands[0]))
            pairs.append((r, cands[0], "doc_no"))
        else:
            only_ledger.append(r)
    # 第二轮：金额 + 行数
    left = [d for d in docs if id(d) not in used]
    still = []
    for r in only_ledger:
        amt = float(r.get("amount") or 0)
        n = int(r.get("lines") or 0)
        cands = [d for d in left
                 if abs(float(d.get("amount") or 0) - amt) <= 0.02
                 and int(d.get("inbound") or 0) == n]
        if cands:
            left.remove(cands[0])
            used.add(id(cands[0]))
            pairs.append((r, cands[0], "amount"))
        else:
            still.append(r)
    return pairs, still, left


# ── 来源分组（界面过滤用，用户 2026-11-25）────────────────────────────
# 三个档位不是「平台名」，是「这批货从哪条路进来的」：
#   梅苗苗 / 牙e在线  能自动拉单的两个平台
#   其他              照片、本地 Excel、供货商直发 —— 都没法自动拉，
#                     所以合成一档：人筛「其他」时想看的就是「不是平台拉的那批」。
# 直接写平台名会漏：origin_label 认不出平台时会退成供货商名（「某某医药有限公司…」），
# 那种行按平台名过滤就整批看不见了。
ORIGIN_KEYS = ("梅苗苗", "牙e在线", "其他")


def origin_key(label):
    """把 origin_label 归到三个过滤档位之一。"""
    s = str(label or "")
    if "梅苗苗" in s:
        return "梅苗苗"
    if "牙e" in s or "牙易" in s:
        return "牙e在线"
    return "其他"


def main(argv=None):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=None, help="出表批次目录（默认最近一个含 decision.json 的）")
    ap.add_argument("--ledger-only", action="store_true", help="只打台账，不核表")
    a = ap.parse_args(argv)

    j = ledger.load()
    records = j.get("records") or []
    print("台账：%d 条（%s）" % (len(records), ledger.LEDGER))
    if a.ledger_only:
        # 最近的排前面（用户 2026-11-25）：台账是流水账，人翻它是想知道「最近出了什么」，
        # 从最早一条往下翻要翻几十屏才看到今天。
        for r in by_order(decorate(records)):
            print("  %-10s %-22s %-8s %s 行  %s 元  %s" % (
                r.get("order_date") or "—", r.get("doc_no"), r.get("origin_label") or "—",
                r.get("lines"), r.get("amount"), r.get("at")))
        return 0
    d = a.dir or latest_dir()
    if not d:
        print("找不到出表批次（没有 decision.json）。先跑一次出表再对账。")
        return 4
    docs, counts, files = load_decision(d)
    print("出表批次：%s" % d)
    print("  单据 %d 份 / 入库表 %s" % (len(docs), files.get("inbound") or "（本批没有）"))
    pairs, only_ledger, only_docs = match(records, docs)
    # 配不上的一侧也补上来源/订单时间，并按最近时间排：这一屏就是人真正要看的清单。
    db = db_index()
    only_ledger = by_order(decorate(only_ledger, docs, db))
    only_docs = by_order(decorate(only_docs, docs, db))
    print("\n对上 %d 条：" % len(pairs))
    for r, doc, how in pairs:
        print("  %-22s -> %-20s %s 元  %d 行  (按%s)" % (
            r.get("doc_no"), doc.get("doc_no"), doc.get("amount"),
            doc.get("inbound") or 0, "单号" if how == "doc_no" else "金额"))
    if only_ledger:
        print("\n台账有、这次表里没有（以前批次出过，或台账记早了）：%d 条" % len(only_ledger))
        for r in only_ledger:
            print("  %-10s %-22s %-8s %s 行  %s 元  %s" % (
                r.get("order_date") or "—", r.get("doc_no"), r.get("origin_label") or "—",
                r.get("lines"), r.get("amount"), r.get("at")))
    if only_docs:
        print("\n表里有、台账没有（用了 --no-ledger 跑的，或台账被删过）：%d 条" % len(only_docs))
        for doc in only_docs:
            print("  %-10s %-22s %-8s %s 元  %d 行  %s" % (
                doc.get("order_date") or "—", doc.get("doc_no"),
                doc.get("origin_label") or "—", doc.get("amount"),
                doc.get("inbound") or 0, doc.get("state") or ""))
    return 0 if not only_ledger else 1


if __name__ == "__main__":
    sys.exit(main())
